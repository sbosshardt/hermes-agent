"""Pinned DKIM retry verification and durable mailbox retry state.

The adapter retains ownership of the native parser, sender gate and IMAP loop.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


def _has_temporary_aligned_dkim_failure(msg, from_addr: str, *, authserv_id: str) -> bool:
    from .adapter import _ar_clauses, _auth_props, _AUTH_METHOD_RE, _domain_of, _domains_aligned

    headers = msg.get_all("Authentication-Results") or []
    from_domain = _domain_of(from_addr)
    if not from_domain or not authserv_id or not headers:
        return False
    clauses = _ar_clauses(" ".join(str(headers[0]).split()))
    if clauses is None or clauses[0].strip().lower() != authserv_id:
        return False
    for clause in clauses[1:]:
        match = _AUTH_METHOD_RE.match(clause)
        if not match or (match.group(1).lower(), match.group(2).lower()) != ("dkim", "temperror"):
            continue
        domains = [value for prop, value in _auth_props(clause[match.end():]) if prop == "header.d"]
        if domains and all(_domains_aligned(domain, from_domain) for domain in domains):
            return True
    return False


def _verify_temporary_dkim(raw_email: bytes, from_addr: str) -> tuple[bool, bool, str]:
    """Reverify original bytes; only timeouts and pure SERVFAIL are transient."""
    from .adapter import _domain_of, _domains_aligned

    try:
        import dkim
        import dns.exception
        import dns.rcode
        import dns.rdatatype
        import dns.resolver
    except ImportError:
        return False, False, "DKIM re-verifier unavailable"
    from_domain = _domain_of(from_addr)
    if not from_domain:
        return False, False, "missing From domain"
    try:
        verifier = dkim.DKIM(raw_email, timeout=5)
    except (TypeError, ValueError, dkim.MessageFormatError):
        return False, False, "invalid message for DKIM re-verification"
    signature_count = sum(name.lower() == b"dkim-signature" for name, _ in verifier.headers)
    dns_timed_out = False
    dns_servfail = False

    def dnsfunc(name: bytes, timeout: int = 5) -> bytes | None:
        nonlocal dns_timed_out, dns_servfail
        try:
            answer = dns.resolver.resolve(name.decode("utf-8"), dns.rdatatype.TXT,
                                          raise_on_no_answer=False, lifetime=timeout, search=True)
            for rrset in answer.response.answer:
                if rrset.rdtype == dns.rdatatype.TXT:
                    return b"".join(list(rrset.items)[0].strings)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoResolverConfiguration, UnicodeDecodeError):
            return None
        except dns.resolver.NoNameservers as exc:
            errors = exc.kwargs.get("errors", [])
            if errors and all(len(error) >= 5 and error[4] is not None
                              and error[4].rcode() == dns.rcode.SERVFAIL for error in errors):
                dns_servfail = True
        except dns.exception.Timeout:
            dns_timed_out = True
            raise dkim.DnsTimeoutError("DNS lookup timed out")
        return None

    for index in range(signature_count):
        try:
            prepared = verifier.verify_headerprep(index)
        except (dkim.MessageFormatError, dkim.ValidationError, dkim.ParameterError):
            continue
        if not prepared:
            continue
        signature, include_headers, signature_headers = prepared
        try:
            signing_domain = signature.get(b"d", b"").decode("ascii")
        except UnicodeDecodeError:
            continue
        if not _domains_aligned(signing_domain, from_domain):
            continue
        try:
            if verifier.verify_sig(signature, include_headers, signature_headers[index], dnsfunc):
                return True, False, f"dkim=pass reverified ({signing_domain})"
        except (dkim.MessageFormatError, dkim.ValidationError, dkim.KeyFormatError,
                dkim.UnknownKeyTypeError, dkim.DnsTimeoutError):
            continue
    if dns_timed_out:
        return False, True, "DKIM DNS lookup timed out"
    if dns_servfail:
        return False, True, "DKIM DNS SERVFAIL"
    return False, False, "DKIM re-verification failed"


class AuthRetryMixin:
    def _init_auth_retry(self) -> None:
        from .adapter import get_hermes_home

        mailbox_key = "\0".join((self._address.strip().lower(), self._imap_host.strip().lower(), str(self._imap_port)))
        mailbox_id = hashlib.sha256(mailbox_key.encode("utf-8")).hexdigest()[:16]
        self._auth_retry_state_path = get_hermes_home() / "state" / f"email_auth_retries-{mailbox_id}.json"
        self._auth_retry_guard_path = self._auth_retry_state_path.with_suffix(".guard")
        self._auth_retry_uidvalidity = ""
        self._auth_retry_generation = 0
        self._pending_auth_retries: set[bytes] = set()
        self._auth_retry_state_error = ""
        # Keep the native reconnect snapshot, but never share it across profiles/mailboxes.
        self._mailbox_snapshot_key = (str(self._auth_retry_state_path.parent), mailbox_key)

    @staticmethod
    def _imap_uidvalidity(imap) -> str:
        try:
            response = imap.response("UIDVALIDITY")
            if not isinstance(response, tuple) or len(response) < 2:
                return ""
            values = response[1]
            if not isinstance(values, (list, tuple)) or not values:
                return ""
            value = values[0]
            if isinstance(value, bytes):
                value = value.decode("ascii", "strict")
            return value.strip() if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit() else ""
        except Exception:
            return ""

    def _auth_retry_mailbox_identity(self) -> dict:
        return {"address": self._address.strip().lower(), "imap_host": self._imap_host.strip().lower(),
                "imap_port": self._imap_port}

    def _set_auth_retry_state_error(self, message: str) -> None:
        self._auth_retry_state_error = message
        logger.error("[Email] %s", message)

    def _sync_auth_retry_state(self, imap) -> None:
        """Validate both durable records before baseline, fetch or acknowledgement."""
        uidvalidity = self._imap_uidvalidity(imap)
        previous_generation = self._auth_retry_generation
        self._auth_retry_state_error = ""
        self._pending_auth_retries = set()
        self._auth_retry_generation = 0
        self._auth_retry_uidvalidity = ""
        try:
            state_exists = self._auth_retry_state_path.exists()
            guard_exists = self._auth_retry_guard_path.exists()
        except OSError:
            self._set_auth_retry_state_error("Cannot inspect authentication retry state safely")
            return
        if state_exists != guard_exists:
            self._set_auth_retry_state_error("Authentication retry state is incomplete; refusing to acknowledge unread mail")
            return
        if not state_exists and previous_generation:
            self._set_auth_retry_state_error("Authentication retry state disappeared; refusing to acknowledge unread mail")
            return
        if not uidvalidity:
            if state_exists:
                self._set_auth_retry_state_error("Cannot validate retained email retry state without IMAP UIDVALIDITY")
            return
        self._auth_retry_uidvalidity = uidvalidity
        if not state_exists:
            return
        try:
            guard = json.loads(self._auth_retry_guard_path.read_text(encoding="utf-8"))
            state = json.loads(self._auth_retry_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            self._set_auth_retry_state_error("Cannot load authentication retry state safely")
            return
        identity = self._auth_retry_mailbox_identity()
        if not isinstance(guard, dict) or guard.get("mailbox") != identity:
            self._set_auth_retry_state_error("Authentication retry guard belongs to a different mailbox")
            return
        if not isinstance(state, dict) or state.get("mailbox") != identity:
            self._set_auth_retry_state_error("Authentication retry state belongs to a different mailbox")
            return
        generation = guard.get("generation")
        if (type(guard.get("version")) is not int or guard["version"] != 1
                or type(state.get("version")) is not int or state["version"] != 3
                or type(generation) is not int or generation < 1
                or type(state.get("generation")) is not int or generation != state["generation"]):
            self._set_auth_retry_state_error("Authentication retry state transaction is inconsistent")
            return
        if state.get("uidvalidity") != uidvalidity:
            self._set_auth_retry_state_error("Authentication retry state UIDVALIDITY changed")
            return
        pending = state.get("pending")
        if not isinstance(pending, list) or any(not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() for uid in pending):
            self._set_auth_retry_state_error("Authentication retry state has invalid pending UIDs")
            return
        self._auth_retry_generation = generation
        self._pending_auth_retries = {uid.encode("ascii") for uid in pending}

    def _save_auth_retry_state(self) -> bool:
        from .adapter import atomic_json_write

        if not self._auth_retry_uidvalidity:
            self._set_auth_retry_state_error("Cannot persist authentication retry state without IMAP UIDVALIDITY")
            return False
        generation = self._auth_retry_generation + 1
        identity = self._auth_retry_mailbox_identity()
        try:
            # A torn pair is deliberately refused on restart, never treated as an empty queue.
            atomic_json_write(self._auth_retry_guard_path,
                              {"version": 1, "mailbox": identity, "generation": generation}, mode=0o600, fsync_dir=True)
            atomic_json_write(self._auth_retry_state_path,
                              {"version": 3, "mailbox": identity, "generation": generation,
                               "uidvalidity": self._auth_retry_uidvalidity,
                               "pending": [uid.decode("ascii") for uid in sorted(self._pending_auth_retries)]},
                              mode=0o600, fsync_dir=True)
        except OSError:
            self._set_auth_retry_state_error("Could not persist authentication retry state safely")
            return False
        self._auth_retry_generation = generation
        return True

    def _initialize_auth_retry_state(self) -> bool:
        if self._auth_retry_state_error:
            return False
        # Native ordinary polling remains supported when a server omits UIDVALIDITY.
        if not self._auth_retry_uidvalidity:
            return True
        if self._auth_retry_generation:
            return True
        return self._save_auth_retry_state()

    def _retain_for_auth_retry(self, uid: bytes) -> bool:
        self._pending_auth_retries.add(uid)
        return self._save_auth_retry_state()

    def _clear_auth_retry(self, uid: bytes) -> bool:
        if uid not in self._pending_auth_retries:
            return True
        self._pending_auth_retries.remove(uid)
        if self._save_auth_retry_state():
            return True
        self._pending_auth_retries.add(uid)
        return False

    def _mark_message_seen(self, imap, uid: bytes) -> bool:
        status, _ = imap.uid("store", uid.decode("ascii"), "+FLAGS.SILENT", r"(\Seen)")
        if status != "OK":
            logger.error("[Email] Could not mark UID %s as seen; deferring dispatch", uid)
            return False
        self._seen_uids.add(uid)
        self._trim_seen_uids()
        return True

    def _retry_failure(self) -> None:
        self._last_fetch_failed = True
        self._last_fetch_error = self._auth_retry_state_error or "Could not persist authentication retry state"

    def _reject_auth_retry(self, imap, uid: bytes) -> None:
        if self._mark_message_seen(imap, uid) and not self._clear_auth_retry(uid):
            self._retry_failure()

    def _process_auth_retry(self, imap, uid: bytes, raw_email: bytes, candidate: dict, preauthorize):
        """Only this branch requires explicit successful STORE before returning a message."""
        from email.parser import BytesHeaderParser
        from .adapter import _verify_temporary_dkim

        wire_meta = self._message_metadata(uid, BytesHeaderParser().parsebytes(raw_email))
        if (wire_meta is None or wire_meta["sender_addr"] != candidate["sender_addr"]
                or not wire_meta["temporary_aligned_dkim_failure"]):
            self._reject_auth_retry(imap, uid)
            return None
        verified, retryable, reason = _verify_temporary_dkim(raw_email, wire_meta["sender_addr"])
        if not verified:
            if retryable:
                if not self._retain_for_auth_retry(uid):
                    self._retry_failure()
            else:
                self._reject_auth_retry(imap, uid)
            return None
        wire_meta.update(sender_authenticated=True, auth_reason=reason)
        if not preauthorize(wire_meta):
            self._reject_auth_retry(imap, uid)
            return None
        if not self._mark_message_seen(imap, uid):
            return None
        if not self._clear_auth_retry(uid):
            self._retry_failure()
            return None
        # Reparse the actual wire payload through the native inbound parser, not synthetic MIME.
        parsed = self._parse_fetched_message(uid, raw_email)
        if parsed is not None:
            parsed.update(sender_authenticated=True, auth_reason=reason)
        return parsed
