"""A codex app-server thread started from scratch is seeded with the session's prior turns (#26035, #74712).

Direction from #26081 (@LeonSGP43). A resumed codex thread already holds the conversation, so only a
fresh thread carries the seed in its first accepted lower-trust user input, never persistent developer
instructions. The recorded prompt composition stays bare so the next turn does not retire the thread.
"""

from types import SimpleNamespace

import pytest

from agent import codex_runtime
from agent.transports import codex_app_server_session as sess_mod
from tests.agent._local_codex_carry_helpers import InertPeer, bounded_turn, inert_codex_boundaries

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


class _FakeClient(InertPeer):
    def __init__(self, **_kw):
        super().__init__()


def _agent(**overrides):
    base = dict(_codex_session=None, session_cwd="/tmp", tool_progress_callback=None,
                _cached_system_prompt="SOUL: you are Hermes", ephemeral_system_prompt=None)
    base.update(overrides)
    return SimpleNamespace(**base)


_HISTORY = [
    {"role": "system", "content": "SOUL: you are Hermes"},
    {"role": "user", "content": "my dog is called Shadow"},
    {"role": "assistant", "content": "Noted: Shadow.", "tool_calls": [{"function": {"name": "memory"}}]},
    {"role": "tool", "content": "saved"},
    {"role": "user", "content": "what is my dog called?"},  # the turn being submitted
]


def test_fresh_thread_is_seeded_with_prior_turns_but_not_the_current_one(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: client)
    agent = _agent()
    codex_runtime._ensure_codex_session(agent, _HISTORY)
    agent._codex_session.ensure_started()
    (_, params), = [(m, p) for (m, p) in client.requests if m == "thread/start"]
    assert params["developerInstructions"] == "SOUL: you are Hermes"
    first = bounded_turn(agent._codex_session, "what is my dog called?")
    assert first.input_accepted is True
    history, current = first.submitted_user_text.rsplit("\n\n[CURRENT USER TURN]\n", 1)
    assert "my dog is called Shadow" in history and "Noted: Shadow." in history
    assert "called tools: memory" in history and "saved" in history
    assert "what is my dog called?" not in history
    assert "SOUL: you are Hermes" not in history
    assert current == "what is my dog called?"
    assert bounded_turn(agent._codex_session, "next question").submitted_user_text == "next question"
    # The seed is not part of the recorded composition: the next turn keeps the thread.
    assert agent._codex_session_prompt == "SOUL: you are Hermes"
    codex_runtime._ensure_codex_session(agent, _HISTORY + [{"role": "assistant", "content": "Shadow"}])
    assert len([m for (m, _) in client.requests if m == "thread/start"]) == 1


def test_resumed_thread_gets_no_seed_but_a_failed_resume_fallback_does(monkeypatch):
    """thread/resume already holds the conversation; the fresh thread started after a failed resume does not."""
    client = _FakeClient()
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: client)
    db = SimpleNamespace(get_session_model_config_value=lambda *_: "stored-thread", patch_session_model_config=lambda *_: None)
    agent = _agent(_session_db=db, session_id="s1", _emit_diagnostic_status=lambda *_: None)
    codex_runtime._ensure_codex_session(agent, _HISTORY)
    codex_runtime._start_codex_thread(agent)
    (_, resume_params), = [(m, p) for (m, p) in client.requests if m == "thread/resume"]
    assert "my dog is called Shadow" not in resume_params.get("developerInstructions", "")

    failing = _FakeClient()
    failing.resume_fails = True
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: failing)
    agent = _agent(_session_db=db, session_id="s1", _emit_diagnostic_status=lambda *_: None)
    codex_runtime._ensure_codex_session(agent, _HISTORY)
    assert codex_runtime._start_codex_thread(agent) == "fresh-thread"
    (_, start_params), = [(m, p) for (m, p) in failing.requests if m == "thread/start"]
    assert start_params["developerInstructions"] == "SOUL: you are Hermes"
    first = bounded_turn(agent._codex_session, "what is my dog called?")
    assert first.input_accepted is True and "my dog is called Shadow" in first.submitted_user_text
    assert first.submitted_user_text.endswith("\n\n[CURRENT USER TURN]\nwhat is my dog called?")
    assert bounded_turn(agent._codex_session, "next question").submitted_user_text == "next question"
