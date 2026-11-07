"""Retained email authentication retry contract; no live IMAP, SMTP or DNS."""
import asyncio
import email
import imaplib
import json
import os
import smtplib
import socket
import subprocess
import sys
from email.mime.text import MIMEText
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def inert_mail(monkeypatch, tmp_path):
    unexpected = []

    def deny(*_a, **_kw):
        caller = sys._getframe(1)
        unexpected.append((caller.f_code.co_filename, caller.f_code.co_name, caller.f_lineno))
        raise AssertionError("non-fixture transport attempted")

    class DeniedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            deny()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, deny)
    monkeypatch.setattr(subprocess, "Popen", DeniedPopen)
    for name in ("system", "popen", "fork", "forkpty", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                 "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, deny)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, deny)
    for name, value in {
        "EMAIL_ADDRESS": "bot@example.test", "EMAIL_PASSWORD": "toy-only",
        "EMAIL_IMAP_HOST": "imap.example.test", "EMAIL_SMTP_HOST": "smtp.example.test",
        "EMAIL_ALLOWED_USERS": "admin@example.test", "EMAIL_ALLOW_ALL_USERS": "false",
        "GATEWAY_ALLOW_ALL_USERS": "false", "EMAIL_TRUST_FROM_HEADER": "false",
    }.items():
        monkeypatch.setenv(name, value)
    for cls in (imaplib.IMAP4, imaplib.IMAP4_SSL, smtplib.SMTP, smtplib.SMTP_SSL):
        monkeypatch.setattr(cls, "open" if cls in (imaplib.IMAP4, imaplib.IMAP4_SSL) else "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        monkeypatch.setattr(socket, name, deny)
    try:
        import dns.resolver
        monkeypatch.setattr(dns.resolver, "resolve", deny)
    except ImportError:
        pass  # Missing pinned dependency is a setup blocker in the signed-DKIM case, not a test skip.
    yield
    assert not unexpected, f"unexpected network/process caller identities: {unexpected}"


def _raw(verdict="temperror", authserv="mx.example.test", domain="example.test"):
    msg = MIMEText("retry me", "plain", "utf-8")
    msg["From"] = "Admin <admin@example.test>"
    msg["Subject"] = "Retry"
    msg["Message-ID"] = "<retry@example.test>"
    msg["Authentication-Results"] = f"{authserv}; dkim={verdict} header.d={domain}; spf=fail smtp.mailfrom=other.test"
    return msg.as_bytes()


def _imap(raw, *, store="OK", uidvalidity=b"4242"):
    from plugins.platforms.email.adapter import _PREAUTH_FETCH

    imap = MagicMock()
    imap.capabilities = ()
    imap.response.return_value = ("UIDVALIDITY", [uidvalidity])
    seen = False

    def uid(command, *args):
        nonlocal seen
        if command == "search":
            return "OK", [b"" if seen else b"7"]
        if command == "fetch":
            if args[-1] == _PREAUTH_FETCH:
                return "OK", [(b"7", raw.split(b"\n\n", 1)[0] + b"\n\n")]
            assert args[-1] in ("(BODY.PEEK[])", "(RFC822)")
            return "OK", [(b"7", raw)]
        if command == "store":
            seen = store == "OK"
            return store, [b"7"]
        raise AssertionError("unexpected fixture IMAP operation")

    imap.uid.side_effect = uid
    return imap


def _adapter(monkeypatch, imap):
    from gateway.config import PlatformConfig
    from plugins.platforms.email.adapter import EmailAdapter
    monkeypatch.setattr(EmailAdapter, "_connect_imap", lambda _self: imap)
    adapter = EmailAdapter(PlatformConfig(enabled=True, extra={"authserv_id": "mx.example.test"}))
    adapter.set_authorization_check(lambda user, kind, chat: user == "admin@example.test" and kind == "dm" and chat == user)
    return adapter


def _fetch(adapter):
    return adapter._fetch_new_messages(lambda candidate: adapter._sender_accepted(candidate["sender_addr"], candidate, for_auth_retry=True))


def test_only_topmost_pinned_aligned_dkim_temperror_nominates_retry():
    from plugins.platforms.email.adapter import _has_temporary_aligned_dkim_failure

    def candidate(raw):
        return _has_temporary_aligned_dkim_failure(email.message_from_bytes(raw), "admin@example.test", authserv_id="mx.example.test")

    assert candidate(_raw())
    for raw in (_raw(authserv="foreign.example.test"), _raw(domain="unrelated.test"),
                _raw(verdict="fail"), _raw().replace(b"Authentication-Results: ", b"X-Other: ")):
        assert not candidate(raw)
    forged = _raw(verdict="fail").replace(
        b"\n\n", b"\nAuthentication-Results: mx.example.test; dkim=temperror header.d=example.test\n\n", 1)
    assert not candidate(forged)


def test_temporary_failure_persists_profile_mailbox_uidvalidity_and_leaves_wire_unseen(monkeypatch):
    from plugins.platforms.email import adapter as module
    imap = _imap(_raw())
    adapter = _adapter(monkeypatch, imap)
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda raw, sender: (False, True, "DNS timeout"))
    assert _fetch(adapter) == []
    assert not any(c.args[0] == "store" for c in imap.uid.call_args_list)
    assert any(c.args[:3] == ("fetch", b"7", "(BODY.PEEK[])") for c in imap.uid.call_args_list)
    state = json.loads(adapter._auth_retry_state_path.read_text())
    guard = json.loads(adapter._auth_retry_guard_path.read_text())
    assert state["mailbox"] == {"address": "bot@example.test", "imap_host": "imap.example.test", "imap_port": 993}
    assert state["uidvalidity"] == "4242" and state["pending"] == ["7"]
    assert state["generation"] == guard["generation"] > 0


def test_restart_retry_rechecks_original_wire_and_acknowledges_before_dispatch(monkeypatch):
    from plugins.platforms.email import adapter as module
    raw = _raw()
    initial, resumed = _imap(raw), _imap(raw)
    first = _adapter(monkeypatch, initial)
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (False, True, "DNS timeout"))
    assert _fetch(first) == []
    second = _adapter(monkeypatch, resumed)
    seen_wire = []

    def reverify(wire, sender):
        seen_wire.append((wire, sender))
        return True, False, "dkim=pass reverified"

    monkeypatch.setattr(module, "_verify_temporary_dkim", reverify)
    messages = _fetch(second)
    assert len(messages) == 1 and messages[0]["sender_authenticated"]
    assert seen_wire == [(raw, "admin@example.test")]
    assert any(c.args[:4] == ("store", "7", "+FLAGS.SILENT", r"(\Seen)") for c in resumed.uid.call_args_list)
    dispatched = []
    async def record(event):
        dispatched.append(event)

    second.handle_message = record
    asyncio.run(second._dispatch_message(messages[0]))
    assert len(dispatched) == 1 and _fetch(second) == []
    assert json.loads(second._auth_retry_state_path.read_text())["pending"] == []


@pytest.mark.parametrize("breakage", ["corrupt", "missing_guard", "wrong_mailbox", "wrong_uidvalidity", "failed_write", "failed_store"])
def test_durable_or_acknowledgement_failure_never_dispatches(monkeypatch, breakage):
    from plugins.platforms.email import adapter as module
    imap = _imap(_raw(), store="NO" if breakage == "failed_store" else "OK")
    adapter = _adapter(monkeypatch, imap)
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (False, True, "DNS timeout"))
    if breakage in {"wrong_uidvalidity", "failed_store"}:
        assert _fetch(adapter) == []
        if breakage == "wrong_uidvalidity":
            imap.response.return_value = ("UIDVALIDITY", [b"4243"])
        adapter = _adapter(monkeypatch, imap)
        monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (True, False, "dkim=pass reverified"))
    elif breakage == "failed_write":
        monkeypatch.setattr(adapter, "_save_auth_retry_state", lambda *a, **k: False)
    elif breakage == "corrupt":
        adapter._auth_retry_state_path.parent.mkdir(parents=True, exist_ok=True)
        adapter._auth_retry_state_path.write_text("{invalid")
    elif breakage == "missing_guard":
        adapter._auth_retry_guard_path.parent.mkdir(parents=True, exist_ok=True)
        adapter._auth_retry_guard_path.write_text(json.dumps({"version": 1, "mailbox": adapter._auth_retry_mailbox_identity(), "generation": 1}))
    elif breakage == "wrong_mailbox":
        adapter._auth_retry_state_path.parent.mkdir(parents=True, exist_ok=True)
        adapter._auth_retry_state_path.write_text(json.dumps({"version": 3, "mailbox": {"address": "other@example.test", "imap_host": "imap.example.test", "imap_port": 993}, "uidvalidity": "4242", "generation": 1, "pending": ["7"]}))
        adapter._auth_retry_guard_path.write_text(json.dumps({"version": 1, "mailbox": adapter._auth_retry_mailbox_identity(), "generation": 1}))
    assert _fetch(adapter) == []
    if breakage != "failed_store":
        assert not any(c.args[0] == "store" for c in imap.uid.call_args_list)
    if breakage == "failed_write":
        assert adapter._last_fetch_failed and b"7" not in adapter._seen_uids
    if breakage in {"corrupt", "missing_guard", "wrong_mailbox", "wrong_uidvalidity"}:
        assert adapter._auth_retry_state_error
    if breakage == "failed_store":
        assert b"7" not in adapter._seen_uids


def test_disconnect_keeps_durable_pending_retry_for_new_adapter(monkeypatch):
    from plugins.platforms.email import adapter as module
    raw = _raw()
    imap = _imap(raw)
    first = _adapter(monkeypatch, imap)
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (False, True, "DNS timeout"))
    assert _fetch(first) == []
    assert json.loads(first._auth_retry_state_path.read_text())["pending"] == ["7"]
    asyncio.run(first.disconnect())
    resumed = _adapter(monkeypatch, _imap(raw))
    assert json.loads(resumed._auth_retry_state_path.read_text())["pending"] == ["7"]
    assert resumed._probe_imap(is_reconnect=False)
    assert b"7" not in resumed._seen_uids
    assert not any(c.args[0] == "store" for c in imap.uid.call_args_list)


def test_revoked_authorization_after_crypto_recheck_never_dispatches(monkeypatch):
    from plugins.platforms.email import adapter as module
    raw = _raw()
    imap = _imap(raw)
    first = _adapter(monkeypatch, imap)
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (False, True, "DNS timeout"))
    assert _fetch(first) == []
    resumed_imap = _imap(raw)
    resumed = _adapter(monkeypatch, resumed_imap)
    monkeypatch.setenv("EMAIL_ALLOWED_USERS", "")
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "")
    granted = {"value": True}
    resumed.set_authorization_check(lambda user, kind, chat: granted["value"])
    def reverify(wire, sender):
        granted["value"] = False
        return True, False, "dkim=pass reverified"
    monkeypatch.setattr(module, "_verify_temporary_dkim", reverify)
    assert _fetch(resumed) == []
    # Rejected mail may be acknowledged as rejected, but not dispatched as authenticated.
    assert any(c.args[0] == "store" for c in resumed_imap.uid.call_args_list)
    assert json.loads(resumed._auth_retry_state_path.read_text())["pending"] == []


def test_failed_durable_clear_after_verified_retry_blocks_dispatch(monkeypatch):
    from plugins.platforms.email import adapter as module
    raw = _raw()
    first = _adapter(monkeypatch, _imap(raw))
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (False, True, "DNS timeout"))
    assert _fetch(first) == []
    resumed_imap = _imap(raw)
    resumed = _adapter(monkeypatch, resumed_imap)
    monkeypatch.setattr(module, "_verify_temporary_dkim", lambda wire, sender: (True, False, "dkim=pass reverified"))
    original_save = resumed._save_auth_retry_state
    def refuse_clear():
        return False if not resumed._pending_auth_retries else original_save()
    monkeypatch.setattr(resumed, "_save_auth_retry_state", refuse_clear)
    assert _fetch(resumed) == []
    assert resumed._last_fetch_failed
    assert any(c.args[0] == "store" for c in resumed_imap.uid.call_args_list)
    assert json.loads(resumed._auth_retry_state_path.read_text())["pending"] == ["7"]


def test_signed_dkim_recheck_distinguishes_timeout_servfail_and_definitive_dns(monkeypatch):
    import base64
    import dkim
    import dns.exception
    import dns.message
    import dns.rcode
    import dns.resolver
    import dns.rdatatype
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from plugins.platforms.email.adapter import _verify_temporary_dkim

    private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    original = b"From: Admin <admin@example.test>\r\nTo: bot@example.test\r\nSubject: retry\r\n\r\nHello\r\n"
    signed = dkim.sign(original, selector=b"retry", domain=b"example.test", privkey=private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()), include_headers=[b"from", b"to", b"subject"]) + original
    record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    answer = MagicMock()
    answer.response.answer = [MagicMock(rdtype=dns.rdatatype.TXT, items=[MagicMock(strings=[record])])]
    monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **k: answer)
    assert _verify_temporary_dkim(signed, "admin@example.test")[:2] == (True, False)
    assert _verify_temporary_dkim(signed.replace(b"Hello", b"Tamper"), "admin@example.test")[:2] == (False, False)
    assert _verify_temporary_dkim(signed, "admin@unrelated.test")[:2] == (False, False)
    for failure, retryable in ((dns.exception.Timeout(), True), (dns.resolver.NXDOMAIN(), False)):
        monkeypatch.setattr(dns.resolver, "resolve", lambda *a, _failure=failure, **k: (_ for _ in ()).throw(_failure))
        assert _verify_temporary_dkim(signed, "admin@example.test")[:2] == (False, retryable)
    request = dns.message.make_query("retry._domainkey.example.test.", "TXT")
    for rcode, retryable in ((dns.rcode.SERVFAIL, True), (dns.rcode.REFUSED, False)):
        response = dns.message.make_response(request)
        response.set_rcode(rcode)
        failure = dns.resolver.NoNameservers(request=request, errors=[("nameserver", False, 53, dns.rcode.to_text(rcode), response)])
        monkeypatch.setattr(dns.resolver, "resolve", lambda *a, _failure=failure, **k: (_ for _ in ()).throw(_failure))
        assert _verify_temporary_dkim(signed, "admin@example.test")[:2] == (False, retryable)
        if rcode == dns.rcode.SERVFAIL:
            assert _verify_temporary_dkim(original, "admin@example.test")[:2] == (False, False)
            assert _verify_temporary_dkim(signed, "admin@unrelated.test")[:2] == (False, False)
    responses = []
    for rcode in (dns.rcode.SERVFAIL, dns.rcode.REFUSED):
        response = dns.message.make_response(request)
        response.set_rcode(rcode)
        responses.append(("nameserver", False, 53, dns.rcode.to_text(rcode), response))
    mixed = dns.resolver.NoNameservers(request=request, errors=responses)
    monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **k: (_ for _ in ()).throw(mixed))
    assert _verify_temporary_dkim(signed, "admin@example.test")[:2] == (False, False)
