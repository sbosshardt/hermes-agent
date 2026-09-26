"""A dedicated bot's Bitwarden login can fill headlessly without a second password store."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.vault_backends import unlock as unlock_state


_FAKE_BW = r'''#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
args = sys.argv[1:]
appdata = Path(os.environ.get("BITWARDENCLI_APPDATA_DIR") or root / "no-cli-state")
token = "fixture-session-" + hashlib.sha256(str(appdata).encode()).hexdigest()[:16]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps({"action": args[0], "password_env": "HERMES_BW_MASTER" in os.environ,
                          "session_env": "BW_SESSION" in os.environ,
                          "appdata": os.environ.get("BITWARDENCLI_APPDATA_DIR"),
                          "argv_has_secret": "fixture-master-secret" in " ".join(args)}) + "\n")
if args[0] == "status":
    print(json.dumps({"status": "locked", "userEmail": (root / "account.txt").read_text().strip()}))
    sys.exit(0)
if args[0] == "unlock":
    if os.environ.get("HERMES_BW_MASTER") != "fixture-master-secret":
        print("Invalid master password", file=sys.stderr); sys.exit(1)
    print(token); sys.exit(0)
if os.environ.get("BW_SESSION") != token:
    print("Vault is locked", file=sys.stderr); sys.exit(1)
if args[0] == "sync":
    if (root / "fail-sync").exists():
        print("Network unavailable", file=sys.stderr); sys.exit(1)
    uris = [] if (root / "remove-uri").exists() else [{"uri": "https://catalog.example.test/login", "match": 0}]
    totp = "test-only-fixture-seed" if (root / "totp-enabled").exists() else None
    pw = (root / "password.txt").read_text().strip() if (root / "password.txt").exists() else "fixture-site-password"
    (appdata / "snapshot.json").write_text(json.dumps({"uris": uris, "totp": totp, "password": pw}))
    print("Sync complete"); sys.exit(0)
snapshot = json.loads((appdata / "snapshot.json").read_text())
if args[:2] == ["list", "items"]:
    print(json.dumps([{"id": "fixture-id", "type": 1, "name": "Agent Site", "collectionIds": ["agent-collection"],
                      "login": {"username": "agent@example.test", "password": snapshot["password"],
                                "uris": snapshot["uris"], "totp": snapshot["totp"]}}])); sys.exit(0)
if args[:3] == ["get", "password", "fixture-id"]:
    print(snapshot["password"]); sys.exit(0)
if args[:3] == ["get", "totp", "fixture-id"]:
    print("123456"); sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def dedicated_bw(tmp_path, monkeypatch):
    from agent.vault_backends import base

    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    exe = tmp_path / "bw"
    exe.write_text(_FAKE_BW)
    exe.chmod(0o700)
    (tmp_path / "account.txt").write_text("henry@example.test")
    secrets_dir = home / "secrets"
    secrets_dir.mkdir(mode=0o700)
    secrets_dir.chmod(0o700)
    pw_file = secrets_dir / "bitwarden.env"
    pw_file.write_text("BW_PASSWORD=fixture-master-secret\n")
    pw_file.chmod(0o600)
    appdata = home / "vault" / "bitwarden-cli"
    appdata.mkdir(parents=True, mode=0o700)
    (home / "vault").chmod(0o700)
    appdata.chmod(0o700)
    cfg = {"bitwarden": {"binary_path": str(exe), "unattended_password_file": str(pw_file),
                         "account_email": "henry@example.test", "appdata_dir": str(appdata)}}
    monkeypatch.setattr(base, "_cfg", lambda: cfg)
    unlock_state.lock("bitwarden")
    yield cfg, pw_file, tmp_path / "calls.jsonl"
    unlock_state.lock("bitwarden")


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_agent_bitwarden_login_lists_and_fills_headlessly_without_a_local_copy(dedicated_bw):
    from agent.vault_store import get_vault_store
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    _cfg, _pw_file, calls_file = dedicated_bw
    with patch("tools.browser_vault_tool._focus_bound_origin", return_value="https://catalog.example.test"), \
         patch("tools.browser_vault_tool._eval_js", return_value={"success": True, "result": json.dumps([
             {"tag": "input", "type": "password", "name": "password", "id": "pw",
              "autocomplete": "current-password", "visible": True}])}), \
         patch("tools.browser_vault_tool._eval_js_secret", return_value={"success": True,
             "result": json.dumps({"filled": 1})}) as fill_js:
        listed = json.loads(browser_vault_list())
        assert not listed.get("errors"), listed.get("errors")
        assert [(item["handle"], item["origin"]) for item in listed["items"]] == [
            ("bw:fixture-id", "https://catalog.example.test")], listed
        assert "fixture-site-password" not in json.dumps(listed)
        result = json.loads(browser_vault_fill("bw:fixture-id", task_id="fixture"))
        assert result["success"] is True and result["backend"] == "bitwarden"
        assert result["origin"] == "https://catalog.example.test"
        assert "fixture-site-password" not in json.dumps(result)
        assert "fixture-site-password" in fill_js.call_args.args[1]
    assert get_vault_store().list_items() == [], "no second Hermes login copy"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert [call["action"] for call in calls].count("unlock") == 1
    assert all(not call["argv_has_secret"] for call in calls)
    assert all(not call["password_env"] for call in calls if call["action"] != "unlock")
    assert all(call["session_env"] for call in calls if call["action"] in {"sync", "list", "get"})
    assert all(call["appdata"] == _cfg["bitwarden"]["appdata_dir"] for call in calls)
    assert "fixture-master-secret" not in calls_file.read_text()


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
@pytest.mark.parametrize("reason", ["outside_home", "other_profile", "loose_mode", "readonly_mode",
                                    "parent_symlink", "shared_cli_state", "wrong_account"])
def test_unattended_bitwarden_fails_closed_before_listing_or_filling(dedicated_bw, reason, tmp_path):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    cfg, pw_file, calls_file = dedicated_bw
    if reason == "outside_home":
        outside = tmp_path / "outside.env"
        outside.write_text("BW_PASSWORD=fixture-master-secret\n")
        outside.chmod(0o600)
        cfg["bitwarden"]["unattended_password_file"] = str(outside)
    elif reason == "other_profile":
        other = pw_file.parents[1] / "profiles" / "another" / "secrets"
        other.mkdir(parents=True)
        other_pw = other / "bitwarden.env"
        other_pw.write_text("BW_PASSWORD=fixture-master-secret\n")
        other_pw.chmod(0o600)
        cfg["bitwarden"]["unattended_password_file"] = str(other_pw)
    elif reason == "loose_mode":
        pw_file.chmod(0o644)
    elif reason == "readonly_mode":
        pw_file.chmod(0o400)
    elif reason == "parent_symlink":
        actual = pw_file.parent.parent / "other-secrets"
        actual.mkdir(mode=0o700)
        actual_pw = actual / "bitwarden.env"
        actual_pw.write_text("BW_PASSWORD=fixture-master-secret\n")
        actual_pw.chmod(0o600)
        link = pw_file.parent / "redirect"
        link.symlink_to(actual, target_is_directory=True)
        cfg["bitwarden"]["unattended_password_file"] = str(link / "bitwarden.env")
    elif reason == "shared_cli_state":
        cfg["bitwarden"]["appdata_dir"] = str(tmp_path / "shared-cli-cache")
    else:
        cfg["bitwarden"]["account_email"] = "another@example.test"
    listed = json.loads(browser_vault_list())
    assert listed["items"] == []
    assert listed["locked"][0]["backend"] == "bitwarden"
    assert listed["errors"] == [{"backend": "bitwarden", "error":
                                 "Unattended vault unlock failed; check the bot account and bootstrap configuration."}]
    filled = json.loads(browser_vault_fill("bw:fixture-id", task_id="fixture"))
    assert filled["success"] is False and filled["error_type"] == "unlock_failed"
    assert "fixture-master-secret" not in json.dumps(listed) + json.dumps(filled)
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
    assert not any(c["action"] in {"unlock", "list", "get"} for c in calls)
    assert (len(calls) == 2 if reason == "wrong_account" else not calls)


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_unattended_password_file_cannot_cross_profile_homes(dedicated_bw, tmp_path, monkeypatch):
    from tools.browser_vault_tool import browser_vault_list

    _cfg, _pw_file, calls_file = dedicated_bw
    first_home = os.environ["HERMES_HOME"]
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:fixture-id"
    calls_before = len(calls_file.read_text().splitlines())
    second_home = tmp_path / "other" / ".hermes"
    second_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(second_home))
    result = json.loads(browser_vault_list())
    assert result["items"] == [] and result["locked"][0]["backend"] == "bitwarden"
    assert len(calls_file.read_text().splitlines()) == calls_before
    monkeypatch.setenv("HERMES_HOME", first_home)
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:fixture-id"


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_preexisting_session_does_not_bypass_configured_account_guard(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_list

    _cfg, _pw_file, calls_file = dedicated_bw
    (tmp_path / "account.txt").write_text("someone-else@example.test")
    unlock_state.store_session_token("bitwarden", "FIXTURE-SESSION")
    result = json.loads(browser_vault_list())
    assert result["items"] == []
    assert result["locked"][0]["backend"] == "bitwarden"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
    assert not any(c["action"] in {"list", "get"} for c in calls)


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_failed_sync_never_fills_a_stale_bitwarden_password(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_fill

    _cfg, _pw_file, calls_file = dedicated_bw
    (tmp_path / "fail-sync").touch()
    result = json.loads(browser_vault_fill("bw:fixture-id", task_id="fixture"))
    assert result["success"] is False and result["error_type"] == "vault_unavailable"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert not any(c["action"] == "get" for c in calls)


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_dedicated_bitwarden_mode_does_not_onboard_a_second_local_copy(dedicated_bw):
    from agent.vault_store import get_vault_store
    from tools.browser_vault_tool import browser_vault_save_login

    with patch("tools.browser_vault_tool._focus_bound_origin", return_value="https://catalog.example.test"), \
         patch("tools.browser_vault_tool._current_page_origin", return_value="https://catalog.example.test"):
        result = json.loads(browser_vault_save_login())
    assert result["success"] is False
    assert result["error_type"] == "bitwarden_source_of_truth"
    assert get_vault_store().list_items() == []


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_each_profile_uses_its_own_cli_appdata_and_session(dedicated_bw, tmp_path, monkeypatch):
    from tools.browser_vault_tool import browser_vault_list

    cfg, first_file, calls_file = dedicated_bw
    first_home = os.environ["HERMES_HOME"]
    assert json.loads(browser_vault_list())["items"][0]["backend"] == "bitwarden"
    second_home = tmp_path / "profile-b"
    second_home.mkdir(mode=0o700)
    second_home.chmod(0o700)
    second_secret_dir = second_home / "secrets"
    second_secret_dir.mkdir(mode=0o700)
    second_file = second_secret_dir / "bitwarden.env"
    second_file.write_text(first_file.read_text())
    second_file.chmod(0o600)
    second_vault = second_home / "vault"
    second_vault.mkdir(mode=0o700)
    second_appdata = second_vault / "bitwarden-cli"
    second_appdata.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(second_home))
    cfg["bitwarden"]["unattended_password_file"] = str(second_file)
    cfg["bitwarden"]["appdata_dir"] = str(second_appdata)
    assert json.loads(browser_vault_list())["items"][0]["backend"] == "bitwarden"
    cfg["bitwarden"]["unattended_password_file"] = str(first_file)
    cfg["bitwarden"]["appdata_dir"] = str(first_home + "/vault/bitwarden-cli")
    monkeypatch.setenv("HERMES_HOME", first_home)
    assert json.loads(browser_vault_list())["items"][0]["backend"] == "bitwarden"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert [c["appdata"] for c in calls if c["action"] == "unlock"] == [
        first_home + "/vault/bitwarden-cli", str(second_appdata)]
    assert all(c["appdata"] in {first_home + "/vault/bitwarden-cli", str(second_appdata)} for c in calls)


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_fill_rechecks_uri_when_it_was_removed_since_listing(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    _cfg, _file, calls_file = dedicated_bw
    assert json.loads(browser_vault_list())["items"][0]["origin"] == "https://catalog.example.test"
    (tmp_path / "remove-uri").touch()
    with patch("tools.browser_vault_tool._focus_bound_origin", return_value="https://catalog.example.test"), \
         patch("tools.browser_vault_tool._eval_js_secret") as fill_js:
        result = json.loads(browser_vault_fill("bw:fixture-id", task_id="fixture"))
    assert result["success"] is False
    assert not fill_js.called
    assert not any(json.loads(line)["action"] == "get" for line in calls_file.read_text().splitlines())


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_fill_uses_rotated_password_after_listing(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    assert json.loads(browser_vault_list())["items"][0]["backend"] == "bitwarden"
    (tmp_path / "password.txt").write_text("fixture-rotated-password")
    with patch("tools.browser_vault_tool._focus_bound_origin", return_value="https://catalog.example.test"), \
         patch("tools.browser_vault_tool._eval_js", return_value={"success": True, "result": json.dumps([
             {"tag": "input", "type": "password", "name": "password", "id": "pw",
              "autocomplete": "current-password", "visible": True}])}), \
         patch("tools.browser_vault_tool._eval_js_secret", return_value={"success": True,
             "result": json.dumps({"filled": 1})}) as fill_js:
        result = json.loads(browser_vault_fill("bw:fixture-id", task_id="fixture"))
    assert result["success"] is True and "fixture-rotated-password" not in json.dumps(result)
    assert "fixture-rotated-password" in fill_js.call_args.args[1]


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_bitwarden_totp_refuses_a_different_website_even_when_code_field_exists(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_enter_code, browser_vault_list

    _cfg, _pw_file, calls_file = dedicated_bw
    (tmp_path / "totp-enabled").touch()
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:fixture-id"
    with patch("tools.browser_vault_tool._current_page_origin", return_value="https://unrelated.example.test"), \
         patch("tools.browser_vault_tool._eval_js", return_value={"success": True, "result": json.dumps([
             {"index": 0, "type": "text", "name": "otp", "label": "Authentication code",
              "autocomplete": "one-time-code"}])}), \
         patch("tools.browser_vault_tool._eval_js_secret") as fill_js:
        result = json.loads(browser_vault_enter_code("bw:fixture-id", task_id="fixture"))
    assert result["success"] is False and result.get("error_type") == "origin_mismatch", result
    assert not fill_js.called
    assert not any(json.loads(line)["action"] == "get" for line in calls_file.read_text().splitlines())


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_bitwarden_list_identifies_an_available_totp_seed(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_list

    (tmp_path / "totp-enabled").touch()
    result = json.loads(browser_vault_list())
    assert result["items"][0]["two_factor"] == "automatic"


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_account_switch_after_metadata_before_password_refuses_fill(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    _cfg, _pw_file, calls_file = dedicated_bw
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:fixture-id"

    def inspect_then_switch_account(_task, _expr):
        (tmp_path / "account.txt").write_text("different-bot@example.test")
        return {"success": True, "result": json.dumps([
            {"index": 0, "type": "password", "name": "password", "autocomplete": "current-password"}])}

    with patch("tools.browser_vault_tool._focus_bound_origin", return_value="https://catalog.example.test"), \
         patch("tools.browser_vault_tool._eval_js", side_effect=inspect_then_switch_account), \
         patch("tools.browser_vault_tool._eval_js_secret") as fill_js:
        result = json.loads(browser_vault_fill("bw:fixture-id", task_id="fixture"))
    assert result["success"] is False and result["error_type"] == "unlock_required"
    assert not fill_js.called
    assert not any(json.loads(line)["action"] == "get" for line in calls_file.read_text().splitlines())


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX shebang")
def test_bitwarden_totp_enters_code_only_on_its_saved_origin(dedicated_bw, tmp_path):
    from tools.browser_vault_tool import browser_vault_enter_code, browser_vault_list

    (tmp_path / "totp-enabled").touch()
    assert json.loads(browser_vault_list())["items"][0]["two_factor"] == "automatic"
    with patch("tools.browser_vault_tool._current_page_origin", return_value="https://catalog.example.test"), \
         patch("tools.browser_vault_tool._eval_js", return_value={"success": True, "result": json.dumps([
             {"index": 0, "type": "text", "name": "otp", "label": "Authentication code",
              "autocomplete": "one-time-code"}])}), \
         patch("tools.browser_vault_tool._eval_js_secret", return_value={"success": True,
             "result": json.dumps({"filled": 1})}) as fill_js:
        result = json.loads(browser_vault_enter_code("bw:fixture-id", task_id="fixture"))
    assert result["success"] is True and result["source"] == "bitwarden"
    assert "123456" not in json.dumps(result)
    assert '"value": "123456"' in fill_js.call_args.args[1]
