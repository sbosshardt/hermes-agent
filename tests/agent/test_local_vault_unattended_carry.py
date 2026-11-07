"""Unattended Bitwarden origin-safe browser fill contract (inert CLI/page unit harness)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys

import pytest


@pytest.fixture
def bot_vault(tmp_path, monkeypatch):
    from agent.vault_backends import base, unlock
    from tools import browser_vault_tool as browser

    home = tmp_path / "bot-home"
    home.mkdir(mode=0o700)
    (home / "secrets").mkdir(mode=0o700)
    (home / "vault").mkdir(mode=0o700)
    appdata = home / "vault" / "bitwarden-cli"
    appdata.mkdir(mode=0o700)
    password_file = home / "secrets" / "master.env"
    password_file.write_text("BW_PASSWORD=toy-only-master\n")
    password_file.chmod(0o600)
    binary = tmp_path / "toy-bw"  # Never executable; subprocess.run is intercepted below.
    binary.touch(mode=0o600)
    config = {"onepassword": {"enabled": False}, "bitwarden": {
        "binary_path": str(binary), "unattended_password_file": str(password_file),
        "account_email": "bot@example.test", "appdata_dir": str(appdata),
    }}
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    monkeypatch.setattr(base, "_cfg", lambda: config)
    unlock.lock("bitwarden")
    events = []
    unexpected = []
    state = {"account": "bot@example.test", "password": "toy-site-one", "uri": "https://catalog.example.test/login",
             "sync_ok": True, "otp": True}

    def deny(*_args, **_kwargs):
        caller = sys._getframe(1)
        unexpected.append((caller.f_code.co_filename, caller.f_code.co_name, caller.f_lineno))
        raise AssertionError("unexpected process/network/browser transport")

    class DeniedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            deny()

    def fake_cli(argv, **kwargs):
        if not isinstance(argv, (list, tuple)) or not argv or argv[0] != str(binary):
            return deny()
        if (kwargs.get("timeout") != 30.0
                or kwargs.get("env", {}).get("BITWARDENCLI_APPDATA_DIR") != str(appdata)):
            return deny()
        args = list(argv[1:])
        allowed = {
            ("status", "--nointeraction"),
            ("unlock", "--raw", "--nointeraction", "--passwordenv", "HERMES_BW_MASTER"),
            ("sync", "--nointeraction"),
            ("list", "items", "--nointeraction"),
            ("get", "password", "toy-id", "--nointeraction"),
            ("get", "totp", "toy-id", "--nointeraction"),
        }
        if tuple(args) not in allowed:
            return deny()
        action = args[0]
        events.append({"action": action, "argv_safe": "toy-only-master" not in " ".join(args),
                       "master_env": kwargs.get("env", {}).get("HERMES_BW_MASTER") == "toy-only-master",
                       "session": bool(kwargs.get("env", {}).get("BW_SESSION"))})
        if action == "status":
            stdout, returncode = json.dumps({"status": "locked", "userEmail": state["account"]}), 0
        elif action == "unlock":
            stdout, returncode = ("toy-session", 0) if events[-1]["master_env"] else ("", 1)
        elif not events[-1]["session"]:
            stdout, returncode = "", 1
        elif action == "sync":
            stdout, returncode = ("Synced", 0) if state["sync_ok"] else ("", 1)
        elif action == "list":
            item = {"id": "toy-id", "type": 1, "name": "Bot Catalog", "login": {
                "username": "bot@example.test", "password": state["password"],
                "uris": [{"uri": state["uri"], "match": 0}] if state["uri"] else [],
                "totp": "toy-seed" if state["otp"] else None}}
            stdout, returncode = json.dumps([item]), 0
        else:
            stdout, returncode = ("123456" if args[1] == "totp" else state["password"]), 0
        return subprocess.CompletedProcess(argv, returncode, stdout, "" if returncode == 0 else "toy CLI unavailable")

    monkeypatch.setattr(subprocess, "run", fake_cli)
    for name in ("call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, deny)
    monkeypatch.setattr(subprocess, "Popen", DeniedPopen)
    for name in ("system", "popen", "fork", "forkpty", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                 "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, deny)
    for name in ("create_subprocess_exec", "create_subprocess_shell"):
        monkeypatch.setattr(asyncio, name, deny)
    monkeypatch.setattr(shutil, "which", deny)  # never locate the real manager
    monkeypatch.setattr(socket, "create_connection", deny)
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(browser, "_page_backend", lambda _task: None)
    monkeypatch.setattr(browser, "_ensure_supervisor", deny)
    monkeypatch.setattr(browser, "_focus_bound_origin", lambda _task, _origin, _kind: None)
    monkeypatch.setattr(browser, "_current_page_origin", lambda _task: state.get("page", "https://catalog.example.test"))
    monkeypatch.setattr(browser, "_eval_js", lambda _task, _script: {"success": True, "result": json.dumps([
        {"index": 0, "tag": "input", "type": "password", "name": "password", "autocomplete": "current-password", "visible": True},
        {"index": 1, "tag": "input", "type": "text", "name": "otp", "autocomplete": "one-time-code", "visible": True}])})
    writes = []

    def secret_eval(_task, script):
        writes.append(script)
        if state.get("navigated"):
            return {"success": True, "result": json.dumps({"refused": "origin_changed", "found": "https://different.test"})}
        return {"success": True, "result": json.dumps({"filled": 1})}

    monkeypatch.setattr(browser, "_eval_js_secret", secret_eval)
    yield config, state, events, writes, password_file, unexpected
    unlock.lock("bitwarden")
    assert not unexpected, f"unexpected process/network/browser caller identities: {unexpected}"


def test_headless_opt_in_lists_then_fills_rotated_value_without_local_copy(bot_vault):
    from agent.vault_store import get_vault_store
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    _cfg, state, events, writes, _file, _unexpected = bot_vault
    listed = json.loads(browser_vault_list())
    assert [(item["handle"], item["origin"]) for item in listed["items"]] == [("bw:toy-id", "https://catalog.example.test")]
    assert "toy-site-one" not in json.dumps(listed)
    state["password"] = "toy-site-rotated"
    result = json.loads(browser_vault_fill("bw:toy-id", task_id="fixture"))
    assert result["success"] and result["origin"] == "https://catalog.example.test"
    assert "toy-site-rotated" not in json.dumps(result) and "toy-site-rotated" in writes[0]
    assert get_vault_store().list_items() == []
    assert [event["action"] for event in events].count("unlock") == 1
    assert [event["action"] for event in events].count("sync") >= 2
    assert all(event["argv_safe"] for event in events)
    assert all(not event["master_env"] for event in events if event["action"] != "unlock")


@pytest.mark.parametrize("fault", ["disabled", "outside_home", "symlink", "loose_mode", "wrong_account", "shared_appdata"])
def test_unattended_bootstrap_rejects_unsafe_configuration_before_secret_use(bot_vault, tmp_path, fault):
    from tools.browser_vault_tool import browser_vault_list

    cfg, state, events, _writes, password_file, _unexpected = bot_vault
    if fault == "disabled":
        cfg["bitwarden"].pop("unattended_password_file")
    elif fault == "outside_home":
        outside = tmp_path / "external.env"
        outside.write_text(password_file.read_text())
        outside.chmod(0o600)
        cfg["bitwarden"]["unattended_password_file"] = str(outside)
    elif fault == "symlink":
        link = password_file.with_name("master-link.env")
        link.symlink_to(password_file)
        cfg["bitwarden"]["unattended_password_file"] = str(link)
    elif fault == "loose_mode":
        password_file.chmod(0o644)
    elif fault == "wrong_account":
        state["account"] = "another-bot@example.test"
    else:
        cfg["bitwarden"]["appdata_dir"] = str(tmp_path / "shared-cli")
    listed = json.loads(browser_vault_list())
    assert not listed["items"]
    assert listed["locked"][0]["backend"] == "bitwarden"
    assert not any(e["action"] in {"unlock", "list", "get"} for e in events)


def test_sync_failure_refuses_stale_password_and_uri_removal_is_checked_again(bot_vault):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    _cfg, state, events, writes, _file, _unexpected = bot_vault
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:toy-id"
    state["sync_ok"] = False
    assert not json.loads(browser_vault_fill("bw:toy-id", task_id="fixture"))["success"]
    assert not writes and not any(e["action"] == "get" for e in events)
    state["sync_ok"] = True
    state["uri"] = ""
    assert not json.loads(browser_vault_fill("bw:toy-id", task_id="fixture"))["success"]
    assert not writes and not any(e["action"] == "get" for e in events)


def test_wrong_origin_refused_before_otp_mint_through_headless_backend(bot_vault):
    from tools.browser_vault_tool import browser_vault_enter_code, browser_vault_list

    _cfg, state, events, writes, _file, _unexpected = bot_vault
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:toy-id"
    state["page"] = "https://unrelated.test"
    before = len(events)
    result = json.loads(browser_vault_enter_code("bw:toy-id", task_id="fixture"))
    assert result["success"] is False and result["error_type"] == "origin_mismatch"
    assert not writes
    assert not any(e["action"] == "get" for e in events[before:])


def test_page_navigation_after_origin_check_is_refused_at_in_page_write(bot_vault):
    from tools.browser_vault_tool import browser_vault_fill, browser_vault_list

    _cfg, state, _events, writes, _file, _unexpected = bot_vault
    assert json.loads(browser_vault_list())["items"][0]["handle"] == "bw:toy-id"
    state["navigated"] = True
    result = json.loads(browser_vault_fill("bw:toy-id", task_id="fixture"))
    assert result["success"] is False and result["error_type"] == "origin_changed"
    assert len(writes) == 1  # unit page fixture simulates the real in-page guard's refusal


def test_unattended_bot_does_not_offer_a_duplicate_local_save(bot_vault):
    from agent.vault_store import get_vault_store
    from tools.browser_vault_tool import browser_vault_save_login

    result = json.loads(browser_vault_save_login(task_id="fixture"))
    assert result["success"] is False and result["error_type"] == "bitwarden_source_of_truth"
    assert get_vault_store().list_items() == []
