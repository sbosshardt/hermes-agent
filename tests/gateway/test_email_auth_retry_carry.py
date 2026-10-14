"""Established local DKIM retry cases, preserving the target email auth/parser suite."""
import os, asyncio, json, tempfile, unittest
from pathlib import Path
from email.mime.text import MIMEText
from unittest.mock import patch, MagicMock, AsyncMock

class TestTemporaryAuthenticationRetry(unittest.TestCase):
    """Temporary DKIM DNS failures must remain safely retryable."""

    def setUp(self):
        # No live mail or DNS fallback, even before a case's narrower mock.
        for target in (
            "imaplib.IMAP4", "imaplib.IMAP4_SSL", "smtplib.SMTP", "smtplib.SMTP_SSL",
            "plugins.platforms.email.adapter._IPv4SMTP",
            "plugins.platforms.email.adapter._IPv4SMTP_SSL",
            "socket.create_connection", "dns.resolver.resolve",
        ):
            transport_patch = patch(target, side_effect=AssertionError("Unexpected non-fixture transport"))
            transport_patch.start()
            self.addCleanup(transport_patch.stop)
        policy_patch = patch.dict(os.environ, {
            "EMAIL_TRUST_FROM_HEADER": "false",
            "EMAIL_ALLOW_ALL_USERS": "false", "EMAIL_ALLOWED_USERS": "",
            "GATEWAY_ALLOW_ALL_USERS": "false", "GATEWAY_ALLOWED_USERS": "",
            "EMAIL_IMAP_SECURITY": "tls", "EMAIL_IMAP_PORT": "993",
            "EMAIL_SMTP_SECURITY": "starttls", "EMAIL_SMTP_PORT": "587",
        }, clear=False)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        # Fatal-status writes use an asynchronous worker. Without isolating it,
        # the worker can create a file under a TemporaryDirectory while its
        # cleanup is removing that directory (sporadic ENOTEMPTY).
        status_patch = patch("gateway.status.publish_runtime_status")
        status_patch.start()
        self.addCleanup(status_patch.stop)

    def _make_adapter(self):
        from gateway.config import PlatformConfig
        from plugins.platforms.email.adapter import EmailAdapter, _get_secret
        adapter = EmailAdapter(PlatformConfig(enabled=True, extra={"authserv_id": "mx.example.net"}))
        self.assertTrue(adapter._require_authenticated_sender)

        def authorize(user_id, chat_type, chat_id):
            # Only this fixture's scoped sender grant; authentication still runs.
            return bool(
                user_id == "admin@example.com" and chat_type == "dm" and chat_id == user_id
                and (_get_secret("EMAIL_ALLOWED_USERS", "").strip() == user_id
                     or adapter._allow_all_senders())
            )

        adapter.set_authorization_check(MagicMock(side_effect=authorize))
        adapter.handle_message = AsyncMock()
        return adapter

    @staticmethod
    def _fetch(adapter):
        # Use the real target gate for mandatory bounded-header preauthorization.
        return adapter._fetch_new_messages(
            lambda candidate: adapter._sender_accepted(
                candidate["sender_addr"], candidate, for_auth_retry=True
            )
        )

    @staticmethod
    def _temporary_dkim_message():
        msg = MIMEText("retry me", "plain", "utf-8")
        msg["From"] = "Admin <admin@example.com>"
        msg["Subject"] = "Retry after DNS recovery"
        msg["Message-ID"] = "<temporary-auth@example.com>"
        msg["Authentication-Results"] = (
            "mx.example.net; dkim=temperror header.d=example.com; "
            "spf=temperror smtp.mailfrom=admin@example.com; "
            "dmarc=temperror header.from=example.com"
        )
        return msg.as_bytes()

    @staticmethod
    def _imap_for(raw_email):
        imap = MagicMock()
        imap.response.return_value = ("UIDVALIDITY", [b"4242"])
        seen = False

        def uid_handler(command, *args):
            nonlocal seen
            if command == "search":
                return ("OK", [b"" if seen else b"7"])
            if command == "fetch":
                from plugins.platforms.email.adapter import _PREAUTH_FETCH
                if args[1] == _PREAUTH_FETCH:
                    headers = raw_email.replace(b"\r\n", b"\n").split(b"\n\n", 1)[0] + b"\n\n"
                    return ("OK", [(b"7", headers)])
                if args[1] not in ("(BODY.PEEK[])", "(RFC822)"):
                    raise AssertionError("Unexpected fixture FETCH")
                return ("OK", [(b"7", raw_email)])
            if command == "store":
                seen = True
                return ("OK", [b"7"])
            return ("NO", [])

        imap.uid.side_effect = uid_handler
        return imap

    def test_temporary_dkim_failure_stays_unseen_and_persists_for_retry(self):
        """A trusted aligned DKIM temperror is not dispatched or silently lost."""
        from plugins.platforms.email import adapter as email_adapter
        imap = self._imap_for(self._temporary_dkim_message())
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {
                 "HERMES_HOME": home,
                 "EMAIL_ADDRESS": "hermes@test.com",
                 "EMAIL_PASSWORD": "secret",
                 "EMAIL_IMAP_HOST": "imap.test.com",
                 "EMAIL_SMTP_HOST": "smtp.test.com",
                 "EMAIL_ALLOWED_USERS": "admin@example.com",
             }, clear=False), \
             patch("plugins.platforms.email.adapter.get_hermes_home", return_value=Path(home), create=True), \
             patch("imaplib.IMAP4_SSL", return_value=imap), \
             patch.object(email_adapter, "atomic_json_write", wraps=email_adapter.atomic_json_write) as writes, \
             patch(
                 "plugins.platforms.email.adapter._verify_temporary_dkim",
                 return_value=(False, True, "DKIM DNS lookup timed out"),
                 create=True,
             ):
            adapter = self._make_adapter()
            results = self._fetch(adapter)
            state_path = adapter._auth_retry_state_path
            with state_path.open(encoding="utf-8") as handle:
                state = json.load(handle)
            with adapter._auth_retry_guard_path.open(encoding="utf-8") as handle:
                guard = json.load(handle)
            self.assertGreaterEqual(writes.call_count, 4)  # bootstrap and retained UID, two records each
            self.assertTrue(all(call.kwargs.get("fsync_dir") is True for call in writes.call_args_list))

        self.assertEqual(results, [])
        self.assertTrue(any(
            call.args == ("fetch", b"7", "(BODY.PEEK[])")
            for call in imap.uid.call_args_list
        ))
        self.assertFalse(any(call.args[0] == "store" for call in imap.uid.call_args_list))
        self.assertEqual(state["uidvalidity"], "4242")
        self.assertEqual(
            state["mailbox"],
            {"address": "hermes@test.com", "imap_host": "imap.test.com", "imap_port": 993},
        )
        self.assertEqual(state["pending"], ["7"])
        self.assertEqual(state["generation"], guard["generation"])
        self.assertGreater(state["generation"], 0)

    def test_executor_poll_uses_own_profile_allowlist_after_a_b_a_switch(self):
        """Scoped retry survives A/B/A; allow-all still requires authenticated From."""
        from agent import secret_scope as ss
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        raw = self._temporary_dkim_message()
        ss.set_multiplex_active(True)
        self.addCleanup(ss.set_multiplex_active, False)
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "HERMES_HOME": root,
            "EMAIL_ADDRESS": "default@test.invalid",
            "EMAIL_PASSWORD": "default-password",
            "EMAIL_IMAP_HOST": "imap.default.invalid",
            "EMAIL_SMTP_HOST": "smtp.default.invalid",
            "EMAIL_ALLOW_ALL_USERS": "true",
            "EMAIL_ALLOWED_USERS": "",
        }, clear=False), patch(
            "plugins.platforms.email.adapter._verify_temporary_dkim",
            return_value=(False, True, "DKIM DNS lookup timed out"),
        ) as verify:
            for label, secrets in (
                ("A", {
                    "EMAIL_ADDRESS": "default@test.invalid", "EMAIL_PASSWORD": "default-password",
                    "EMAIL_IMAP_HOST": "imap.default.invalid", "EMAIL_SMTP_HOST": "smtp.default.invalid",
                    "EMAIL_ALLOW_ALL_USERS": "true",
                }),
                ("B", {
                    "EMAIL_ADDRESS": "secondary@test.invalid", "EMAIL_PASSWORD": "secondary-password",
                    "EMAIL_IMAP_HOST": "imap.secondary.invalid", "EMAIL_SMTP_HOST": "smtp.secondary.invalid",
                    "EMAIL_ALLOWED_USERS": "admin@example.com",
                }),
                ("A", {
                    "EMAIL_ADDRESS": "default@test.invalid", "EMAIL_PASSWORD": "default-password",
                    "EMAIL_IMAP_HOST": "imap.default.invalid", "EMAIL_SMTP_HOST": "smtp.default.invalid",
                    "EMAIL_ALLOW_ALL_USERS": "true",
                }),
            ):
                home = Path(root) / label
                home.mkdir(exist_ok=True)
                scope_token = ss.set_secret_scope(secrets)
                home_token = set_hermes_home_override(home)
                try:
                    # A's allow-all grant cannot excuse a permanent auth failure.
                    # B alone nominates a temporary aligned DKIM retry.
                    profile_raw = raw if label == "B" else raw.replace(b"dkim=temperror", b"dkim=fail")
                    imap = self._imap_for(profile_raw)
                    adapter = self._make_adapter()
                    authorization_check = adapter._authorization_check
                    assert isinstance(authorization_check, MagicMock)
                    adapter._dispatch_message = AsyncMock()
                    with patch("imaplib.IMAP4_SSL", return_value=imap):
                        asyncio.run(adapter._check_inbox())
                    stored = any(call.args[0] == "store" for call in imap.uid.call_args_list)
                    if label == "B":
                        self.assertFalse(stored, "B must not acknowledge A's allow-all policy")
                        self.assertEqual(adapter._pending_auth_retries, {b"7"})
                        self.assertEqual(adapter._auth_retry_state_path.parent, home / "state")
                        adapter._dispatch_message.assert_not_awaited()
                        authorization_check.assert_not_called()
                    else:
                        self.assertTrue(stored)
                        self.assertEqual(adapter._pending_auth_retries, set())
                        authorization_check.assert_called_once_with(
                            "admin@example.com", "dm", "admin@example.com"
                        )
                        adapter._dispatch_message.assert_not_awaited()
                        self.assertFalse(any(
                            call.args[0] == "fetch" and call.args[-1] in ("(BODY.PEEK[])", "(RFC822)")
                            for call in imap.uid.call_args_list
                        ), "allow-all must not fetch a body with unauthenticated From")
                finally:
                    reset_hermes_home_override(home_token)
                    ss.reset_secret_scope(scope_token)
            self.assertEqual(verify.call_count, 1)

    def test_direct_dkim_reverification_requires_an_aligned_valid_signature(self):
        """The retry path independently validates the original raw message."""
        import base64
        import dkim
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from plugins.platforms.email.adapter import _verify_temporary_dkim

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        private_bytes = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        original = (
            b"From: Admin <admin@example.com>\r\n"
            b"To: hermes@example.test\r\n"
            b"Subject: authenticated retry\r\n"
            b"\r\n"
            b"Hello\r\n"
        )
        signed = dkim.sign(
            original,
            selector=b"retry",
            domain=b"example.com",
            privkey=private_bytes,
            include_headers=[b"from", b"to", b"subject"],
        ) + original
        key_record = b"v=DKIM1; k=rsa; p=" + base64.b64encode(public_der)
        import dns.rdatatype
        txt_rrset = MagicMock(rdtype=dns.rdatatype.TXT, items=[MagicMock(strings=[key_record])])
        dns_answer = MagicMock()
        dns_answer.response.answer = [txt_rrset]

        with patch("dns.resolver.resolve", return_value=dns_answer):
            verified, retryable, reason = _verify_temporary_dkim(
                signed, "admin@example.com"
            )

        self.assertTrue(verified, reason)
        self.assertFalse(retryable)
        self.assertIn("dkim=pass", reason)

        # Authentication must be over the fetched wire bytes, not the parsed
        # or reserialized message: even one modified body byte invalidates it.
        with patch("dns.resolver.resolve", return_value=dns_answer):
            verified, retryable, _reason = _verify_temporary_dkim(
                signed.replace(b"Hello\r\n", b"Tampered\r\n"), "admin@example.com"
            )
        self.assertFalse(verified)
        self.assertFalse(retryable)

        # A cryptographically valid signature for another From domain is not
        # evidence that the allowlisted sender is authenticated.
        with patch("dns.resolver.resolve", return_value=dns_answer) as resolve:
            verified, retryable, _reason = _verify_temporary_dkim(
                signed, "admin@unrelated.test"
            )
            resolve.assert_not_called()
        self.assertFalse(verified)
        self.assertFalse(retryable)

        import dns.exception
        with patch("dns.resolver.resolve", side_effect=dns.exception.Timeout("DNS timeout")):
            verified, retryable, reason = _verify_temporary_dkim(
                signed, "admin@example.com"
            )
        self.assertFalse(verified)
        self.assertTrue(retryable)
        self.assertIn("DNS", reason)

        # dkimpy 1.1.8 silently maps dns.resolver.NoNameservers (including
        # SERVFAIL) to a missing key; only a definitive SERVFAIL response is
        # transient. Other DNS failures must not become retryable.
        import dns.message
        import dns.rcode
        import dns.resolver
        request = dns.message.make_query("retry._domainkey.example.com.", "TXT")
        for rcodes, expected_retry in (
            ((dns.rcode.SERVFAIL,), True),
            ((dns.rcode.REFUSED,), False),
            ((dns.rcode.SERVFAIL, dns.rcode.REFUSED), False),
        ):
            with self.subTest(rcodes=[dns.rcode.to_text(code) for code in rcodes]):
                errors = []
                for code in rcodes:
                    response = dns.message.make_response(request)
                    response.set_rcode(code)
                    errors.append(("nameserver", False, 53, dns.rcode.to_text(code), response))
                error = dns.resolver.NoNameservers(request=request, errors=errors)
                with patch("dns.resolver.resolve", side_effect=error):
                    verified, retryable, _reason = _verify_temporary_dkim(
                        signed, "admin@example.com"
                    )
                self.assertFalse(verified)
                self.assertEqual(retryable, expected_retry)

        # SERVFAIL cannot turn an unsigned or misaligned message into a retry.
        servfail_response = dns.message.make_response(request)
        servfail_response.set_rcode(dns.rcode.SERVFAIL)
        servfail = dns.resolver.NoNameservers(
            request=request,
            errors=[("nameserver", False, 53, "SERVFAIL", servfail_response)],
        )
        with patch("dns.resolver.resolve", side_effect=servfail) as resolve:
            self.assertEqual(
                _verify_temporary_dkim(original, "admin@example.com")[:2],
                (False, False),
            )
            self.assertEqual(
                _verify_temporary_dkim(signed, "admin@unrelated.test")[:2],
                (False, False),
            )
            resolve.assert_not_called()

        with patch("dns.resolver.resolve", side_effect=dns.resolver.NXDOMAIN()):
            verified, retryable, _reason = _verify_temporary_dkim(
                signed, "admin@example.com"
            )
        self.assertFalse(verified)
        self.assertFalse(retryable)

    def test_persisted_retry_reauthenticates_and_dispatches_at_most_once(self):
        """A fresh adapter resumes the queued UID, then acknowledges before dispatch."""
        raw_email = self._temporary_dkim_message()
        initial_imap = self._imap_for(raw_email)
        retry_imap = self._imap_for(raw_email)
        dispatched = []

        async def capture(event):
            # The server acknowledgement must precede user-code dispatch.
            self.assertTrue(any(
                call.args[:4] == ("store", "7", "+FLAGS.SILENT", r"(\Seen)")
                for call in retry_imap.uid.call_args_list
            ))
            dispatched.append(event)

        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {
                 "HERMES_HOME": home,
                 "EMAIL_ADDRESS": "hermes@test.com",
                 "EMAIL_PASSWORD": "secret",
                 "EMAIL_IMAP_HOST": "imap.test.com",
                 "EMAIL_SMTP_HOST": "smtp.test.com",
                 "EMAIL_ALLOWED_USERS": "admin@example.com",
             }, clear=False), \
             patch("plugins.platforms.email.adapter.get_hermes_home", return_value=Path(home), create=True):
            with patch("imaplib.IMAP4_SSL", return_value=initial_imap), \
                 patch(
                     "plugins.platforms.email.adapter._verify_temporary_dkim",
                     return_value=(False, True, "DKIM DNS lookup timed out"),
                 create=True,
                 ):
                first_adapter = self._make_adapter()
                self.assertEqual(self._fetch(first_adapter), [])

            with patch("imaplib.IMAP4_SSL", return_value=retry_imap), \
                 patch(
                     "plugins.platforms.email.adapter._verify_temporary_dkim",
                     return_value=(True, False, "dkim=pass reverified (example.com)"),
                     create=True,
                 ):
                retry_adapter = self._make_adapter()
                messages = self._fetch(retry_adapter)
                self.assertEqual(len(messages), 1)
                self.assertTrue(messages[0]["sender_authenticated"])
                self.assertIn("reverified", messages[0]["auth_reason"])
                retry_adapter.handle_message = capture
                asyncio.run(retry_adapter._dispatch_message(messages[0]))
                self.assertEqual(self._fetch(retry_adapter), [])

            state_path = retry_adapter._auth_retry_state_path
            with state_path.open(encoding="utf-8") as handle:
                state = json.load(handle)
            with retry_adapter._auth_retry_guard_path.open(encoding="utf-8") as handle:
                guard = json.load(handle)

        self.assertEqual(len(dispatched), 1)
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["generation"], guard["generation"])
        self.assertEqual(
            sum(call.args[0] == "store" for call in retry_imap.uid.call_args_list),
            1,
        )

    def test_corrupt_retry_state_blocks_startup_before_bootstrap(self):
        """Never mark all mail seen when a retained retry record is unreadable."""
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {
                 "HERMES_HOME": home,
                 "EMAIL_ADDRESS": "hermes@test.com",
                 "EMAIL_PASSWORD": "secret",
                 "EMAIL_IMAP_HOST": "imap.test.com",
                 "EMAIL_SMTP_HOST": "smtp.test.com",
             }, clear=False), \
             patch("plugins.platforms.email.adapter.get_hermes_home", return_value=Path(home), create=True):
            adapter = self._make_adapter()
            adapter._auth_retry_state_path.parent.mkdir(parents=True, exist_ok=True)
            adapter._auth_retry_state_path.write_text("{not valid JSON", encoding="utf-8")
            adapter._auth_retry_guard_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mailbox": adapter._auth_retry_mailbox_identity(),
                        "generation": 1,
                    }
                ),
                encoding="utf-8",
            )
            imap = MagicMock()
            imap.response.return_value = ("UIDVALIDITY", [b"4242"])

            with patch("imaplib.IMAP4_SSL", return_value=imap), \
                 patch("smtplib.SMTP"):
                self.assertFalse(asyncio.run(adapter.connect()))

        self.assertEqual(adapter.fatal_error_code, "email_auth_retry_state_error")
        self.assertFalse(adapter.fatal_error_retryable)
        self.assertFalse(any(call.args[0] == "search" for call in imap.uid.call_args_list))

    def test_unknown_retry_state_schema_blocks_startup_before_bootstrap(self):
        """An unrecognized durable-state schema is not safe to interpret."""
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {
                 "HERMES_HOME": home,
                 "EMAIL_ADDRESS": "hermes@test.com",
                 "EMAIL_PASSWORD": "secret",
                 "EMAIL_IMAP_HOST": "imap.test.com",
                 "EMAIL_SMTP_HOST": "smtp.test.com",
             }, clear=False), \
             patch("plugins.platforms.email.adapter.get_hermes_home", return_value=Path(home), create=True):
            adapter = self._make_adapter()
            identity = adapter._auth_retry_mailbox_identity()
            adapter._auth_retry_state_path.parent.mkdir(parents=True, exist_ok=True)
            adapter._auth_retry_state_path.write_text(
                json.dumps(
                    {
                        "version": 999,
                        "mailbox": identity,
                        "generation": 1,
                        "uidvalidity": "4242",
                        "pending": [],
                    }
                ),
                encoding="utf-8",
            )
            adapter._auth_retry_guard_path.write_text(
                json.dumps({"version": 1, "mailbox": identity, "generation": 1}),
                encoding="utf-8",
            )
            imap = MagicMock()
            imap.response.return_value = ("UIDVALIDITY", [b"4242"])

            with patch("imaplib.IMAP4_SSL", return_value=imap), \
                 patch("smtplib.SMTP"):
                self.assertFalse(asyncio.run(adapter.connect()))

        self.assertIn("transaction is inconsistent", adapter._auth_retry_state_error)
        self.assertFalse(any(call.args[0] == "search" for call in imap.uid.call_args_list))

    def test_missing_retry_state_with_persistent_guard_blocks_startup(self):
        """Loss of one retry record cannot turn retained mail into historical mail."""
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {
                 "HERMES_HOME": home,
                 "EMAIL_ADDRESS": "hermes@test.com",
                 "EMAIL_PASSWORD": "secret",
                 "EMAIL_IMAP_HOST": "imap.test.com",
                 "EMAIL_SMTP_HOST": "smtp.test.com",
             }, clear=False), \
             patch("plugins.platforms.email.adapter.get_hermes_home", return_value=Path(home), create=True):
            adapter = self._make_adapter()
            adapter._auth_retry_guard_path.parent.mkdir(parents=True, exist_ok=True)
            adapter._auth_retry_guard_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mailbox": adapter._auth_retry_mailbox_identity(),
                        "generation": 1,
                    }
                ),
                encoding="utf-8",
            )
            imap = MagicMock()
            imap.response.return_value = ("UIDVALIDITY", [b"4242"])

            with patch("imaplib.IMAP4_SSL", return_value=imap), \
                 patch("smtplib.SMTP"):
                self.assertFalse(asyncio.run(adapter.connect()))

        self.assertEqual(adapter.fatal_error_code, "email_auth_retry_state_error")
        self.assertIn("incomplete", adapter._auth_retry_state_error)
        self.assertFalse(any(call.args[0] == "search" for call in imap.uid.call_args_list))

    def test_wrong_mailbox_retry_state_blocks_fetch_without_acknowledging_mail(self):
        """Retry state for another mailbox cannot suppress or consume this inbox."""
        raw_email = self._temporary_dkim_message()
        imap = self._imap_for(raw_email)
        with tempfile.TemporaryDirectory() as home, \
             patch.dict(os.environ, {
                 "HERMES_HOME": home,
                 "EMAIL_ADDRESS": "hermes@test.com",
                 "EMAIL_PASSWORD": "secret",
                 "EMAIL_IMAP_HOST": "imap.test.com",
                 "EMAIL_SMTP_HOST": "smtp.test.com",
                 "EMAIL_ALLOWED_USERS": "admin@example.com",
             }, clear=False), \
             patch("plugins.platforms.email.adapter.get_hermes_home", return_value=Path(home), create=True):
            adapter = self._make_adapter()
            adapter._auth_retry_state_path.parent.mkdir(parents=True, exist_ok=True)
            adapter._auth_retry_state_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "mailbox": {
                            "address": "other@test.com",
                            "imap_host": "imap.test.com",
                            "imap_port": 993,
                        },
                        "uidvalidity": "4242",
                        "generation": 1,
                        "pending": ["7"],
                    }
                ),
                encoding="utf-8",
            )
            adapter._auth_retry_guard_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "mailbox": adapter._auth_retry_mailbox_identity(),
                        "generation": 1,
                    }
                ),
                encoding="utf-8",
            )
            with patch("imaplib.IMAP4_SSL", return_value=imap):
                self.assertEqual(self._fetch(adapter), [])

        self.assertIn("different mailbox", adapter._auth_retry_state_error)
        self.assertFalse(any(call.args[0] == "search" for call in imap.uid.call_args_list))

    def test_forged_lower_authentication_results_cannot_create_retry(self):
        from plugins.platforms.email.adapter import _has_temporary_aligned_dkim_failure
        raw = self._temporary_dkim_message()
        msg = MIMEText("spoofed", "plain")
        msg["From"] = "Admin <admin@example.com>"
        msg["Authentication-Results"] = "mx.example.net; dkim=fail header.d=example.com"
        msg["Authentication-Results"] = "attacker.example; dkim=temperror header.d=example.com"
        self.assertFalse(_has_temporary_aligned_dkim_failure(
            msg, "admin@example.com", authserv_id="mx.example.net"))
        self.assertFalse(_has_temporary_aligned_dkim_failure(
            __import__("email").message_from_bytes(raw), "admin@unrelated.test",
            authserv_id="mx.example.net"))
        self.assertFalse(_has_temporary_aligned_dkim_failure(
            __import__("email").message_from_bytes(raw), "admin@example.com",
            authserv_id="different.test"))

    def test_failed_store_defers_dispatch_without_suppressing_uid(self):
        imap = self._imap_for(self._temporary_dkim_message())
        imap.uid.side_effect = lambda command, *args: (
            ("OK", [b"7"]) if command == "search" else
            ("OK", [(b"7", self._temporary_dkim_message())]) if command == "fetch" else
            ("NO", [])
        )
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {
            "HERMES_HOME": home, "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret", "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com", "EMAIL_ALLOWED_USERS": "admin@example.com",
        }), patch("imaplib.IMAP4_SSL", return_value=imap), patch(
            "plugins.platforms.email.adapter._verify_temporary_dkim",
            return_value=(True, False, "dkim=pass reverified (example.com)"),
        ):
            adapter = self._make_adapter()
            self.assertEqual(self._fetch(adapter), [])
            self.assertNotIn(b"7", adapter._seen_uids)
        self.assertIn("store", [call.args[0] for call in imap.uid.call_args_list])

    def test_persisted_retry_survives_first_connect_baseline(self):
        raw = self._temporary_dkim_message()
        imap = self._imap_for(raw)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {
            "HERMES_HOME": home, "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret", "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com", "EMAIL_ALLOWED_USERS": "admin@example.com",
        }), patch("imaplib.IMAP4_SSL", return_value=imap), patch(
            "plugins.platforms.email.adapter._verify_temporary_dkim",
            return_value=(False, True, "DNS timeout"),
        ):
            first = self._make_adapter()
            self.assertEqual(self._fetch(first), [])
            self.assertIn(b"7", first._pending_auth_retries)
            second = self._make_adapter()
            self.assertTrue(second._probe_imap(is_reconnect=False))
            self.assertNotIn(b"7", second._seen_uids)
            self.assertIn(b"7", second._pending_auth_retries)

    def test_uidvalidity_change_refuses_baseline(self):
        imap = self._imap_for(self._temporary_dkim_message())
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {
            "HERMES_HOME": home, "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret", "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com", "EMAIL_ALLOWED_USERS": "admin@example.com",
        }), patch("imaplib.IMAP4_SSL", return_value=imap), patch(
            "plugins.platforms.email.adapter._verify_temporary_dkim",
            return_value=(False, True, "DNS timeout"),
        ):
            first = self._make_adapter()
            self.assertEqual(self._fetch(first), [])
            imap.uid.reset_mock()
            imap.response.return_value = ("UIDVALIDITY", [b"changed"])
            second = self._make_adapter()
            self.assertFalse(second._probe_imap(is_reconnect=False))
            self.assertEqual(second.fatal_error_code, "email_auth_retry_state_error")
            self.assertFalse(any(c.args[0] == "search" for c in imap.uid.call_args_list))

    def test_state_write_failure_leaves_temporary_mail_unacknowledged(self):
        from plugins.platforms.email.adapter import EmailAdapter
        imap = self._imap_for(self._temporary_dkim_message())
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {
            "HERMES_HOME": home, "EMAIL_ADDRESS": "hermes@test.com",
            "EMAIL_PASSWORD": "secret", "EMAIL_IMAP_HOST": "imap.test.com",
            "EMAIL_SMTP_HOST": "smtp.test.com", "EMAIL_ALLOWED_USERS": "admin@example.com",
        }), patch("imaplib.IMAP4_SSL", return_value=imap), patch(
            "plugins.platforms.email.adapter._verify_temporary_dkim",
            return_value=(False, True, "DNS timeout"),
        ), patch.object(EmailAdapter, "_save_auth_retry_state", return_value=False):
            adapter = self._make_adapter()
            self.assertEqual(self._fetch(adapter), [])
            self.assertTrue(adapter._last_fetch_failed)
            self.assertNotIn(b"7", adapter._seen_uids)
            self.assertFalse(any(c.args[0] == "store" for c in imap.uid.call_args_list))
