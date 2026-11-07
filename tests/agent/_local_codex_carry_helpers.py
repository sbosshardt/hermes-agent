"""Inert Codex RPC peer and disposable DB fixtures for the coupled carry tests.

Only the process/transport is replaced. Session adapter, turn/start ACK,
turn prologue, SessionDB, and history renderer remain native.
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult
from hermes_state import SessionDB
from run_agent import AIAgent


@pytest.fixture
def inert_codex_boundaries(monkeypatch, tmp_path):
    """Fail closed at process/network bottoms even when a runtime fallback runs.

    Each new test module imports and registers this fixture explicitly. The real
    pytest home/SessionDB isolation is unchanged. Only optional environment-probe
    warmup is neutralized: it starts an unrelated subprocess thread in AIAgent init.
    """
    attempts = []

    def refuse(kind):
        def blocked(*args, **kwargs):
            frame = sys._getframe(1)
            callers = []
            for _ in range(12):
                if frame is None:
                    break
                callers.append((Path(frame.f_code.co_filename).name,
                                frame.f_code.co_name, frame.f_lineno))
                frame = frame.f_back
            attempts.append({"kind": kind, "callers": callers})
            raise AssertionError(f"unexpected {kind} in inert Codex test")
        return blocked

    class DeniedPopen(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            refuse("subprocess.Popen")(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", DeniedPopen)
    for name in ("system", "popen", "fork", "posix_spawn", "posix_spawnp", "spawnl", "spawnle",
                 "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse(f"os.{name}"))
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, refuse(f"socket.{name}"))
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        if hasattr(socket, name):
            monkeypatch.setattr(socket, name, refuse(f"socket.{name}"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse("asyncio.create_subprocess_exec"))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse("asyncio.create_subprocess_shell"))
    from tools import env_probe
    monkeypatch.setattr(env_probe, "warm_environment_probe_async", lambda: None)
    monkeypatch.setattr(env_probe, "get_environment_probe_line", lambda **_ignored: "")
    # Native fixture configuration, not detector/guard monkeypatches: avoid the
    # unrelated context-window HTTP autodetection and Git workspace enrichment.
    # Preserve the first failed run and every denial; this fresh test still
    # fails closed if any process or network bottom is unexpectedly reached.
    from hermes_constants import get_hermes_home
    from utils import fast_safe_load
    home = Path(get_hermes_home())
    assert home.resolve().is_relative_to(tmp_path.resolve())
    assert not home.is_symlink()
    config_path = home / "config.yaml"
    assert not config_path.is_symlink()
    config = fast_safe_load(config_path.read_text()) if config_path.exists() else {}
    config = {} if config is None else config
    assert isinstance(config, dict)
    config.setdefault("model", {})["context_length"] = 400000
    config.setdefault("agent", {})["coding_context"] = "off"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    yield
    assert not attempts, f"unexpected process/network attempts: {attempts}"


class InertPeer:
    def __init__(self, ack=None, *, resume_fails=False, fail_first_turn=False):
        self.ack = {"turn": {"id": "accepted-turn"}} if ack is None else ack
        self.resume_fails = resume_fails
        self.fail_first_turn = fail_first_turn
        self.requests = []
        self._turn_requests = 0
        self._closed = False
        self._notifications = []

    def initialize(self, **kwargs):
        return {}

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "fresh-thread"}}
        if method == "thread/resume":
            if self.resume_fails:
                from agent.transports.codex_app_server import CodexAppServerError
                raise CodexAppServerError(code=-32602, message="unknown thread")
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            self._turn_requests += 1
            if self.fail_first_turn and self._turn_requests == 1:
                from agent.transports.codex_app_server import CodexAppServerError
                raise CodexAppServerError(code=-32603, message="inert submit refused")
            response = self.ack if self._turn_requests == 1 else {"turn": {"id": "accepted-turn"}}
            turn = response.get("turn") if isinstance(response, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            if isinstance(turn_id, str) and turn_id.strip():
                self._notifications.append({"method": "item/completed", "params": {
                    "threadId": params["threadId"], "turnId": turn_id,
                    "item": {"type": "agentMessage", "id": "fixture-reply", "text": "done"}}})
                self._notifications.append({"method": "turn/completed", "params": {
                    "threadId": params["threadId"], "turn": {"id": turn_id, "status": "completed"}}})
            return response
        raise AssertionError(f"Unexpected local RPC {method}")

    def take_notification(self, timeout=0):
        return self._notifications.pop(0) if self._notifications else None

    def take_server_request(self, timeout=0):
        return None

    def is_alive(self):
        return not self._closed

    def stderr_tail(self, count):
        return []

    def close(self):
        self._closed = True


def local_session(peer, **kwargs):
    return CodexAppServerSession(
        developer_instructions="TRUSTED HERMES PROMPT",
        client_factory=lambda **_ignored: peer, **kwargs,
    )


def bounded_turn(session, user_input):
    """A malformed-ACK regression must never wait the native 600-second deadline."""
    return session.run_turn(user_input, turn_timeout=0.2, notification_poll_timeout=0)


def local_db_agent(tmp_path, *, session_id="local-codex-carry"):
    db = SessionDB(tmp_path / "state.db")
    try:
        agent = AIAgent(
            api_key="fixture-key", base_url="https://fixture.invalid", provider="openai",
            api_mode="codex_app_server", model="gpt-5-codex", quiet_mode=True, skip_context_files=True,
            skip_memory=True, skip_background_review=True, session_db=db, session_id=session_id,
            reasoning_config={"enabled": True, "effort": "medium"},
            request_overrides={"service_tier": "priority"}, cwd=str(tmp_path),
        )
    except BaseException:
        db.close()
        raise
    return db, agent


def accepted_turn(wire, **overrides):
    turn = TurnResult(final_text="done", thread_id="fresh-thread", turn_id="accepted-turn",
                      submitted_user_text=wire,
                      projected_messages=[{"role": "assistant", "content": "done"}])
    setattr(turn, "input_accepted", overrides.pop("input_accepted", True))
    for name, value in overrides.items():
        setattr(turn, name, value)
    return turn
