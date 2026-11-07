"""C07: current selected context reaches Codex, not visible transcript."""
from unittest.mock import MagicMock
import pytest

from agent import codex_runtime, turn_context
from agent.turn_context import compose_user_api_content
from agent.codex_runtime_history_seed import render_history_seed
from tests.agent._local_codex_carry_helpers import accepted_turn, inert_codex_boundaries, local_db_agent

pytestmark = pytest.mark.usefixtures("inert_codex_boundaries")


def test_prefetched_and_plugin_context_are_wire_only_and_recoverable(tmp_path, monkeypatch):
    db, agent = local_db_agent(tmp_path, session_id="current-context")
    visible = "What did we decide about deployment?"
    recall = "use the reviewed staging candidate"
    plugin = "production cutover stays held"
    sent = []
    memory = MagicMock()
    memory.build_system_prompt.return_value = ""
    memory.wait_for_session_boundary.return_value = True
    memory.describe_recall.return_value = ""
    memory.prefetch_all.return_value = recall
    agent._memory_manager = memory
    monkeypatch.setattr(turn_context, "_maybe_title_session_at_turn_start", lambda *a: None)
    monkeypatch.setattr(turn_context, "_collect_pre_llm_call_context", lambda *a, **kw: plugin)
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda *a: None)

    class RecordingSession:
        _history_seed_pending = False
        _history_seed = None

        def ensure_started(self):
            return "fresh-thread"

        def run_turn(self, user_input, **kwargs):
            sent.append((user_input, kwargs))
            return accepted_turn(user_input)

        def close(self):
            pass

    agent._codex_session = RecordingSession()
    try:
        assert agent.run_conversation(visible)["completed"] is True
        wire = compose_user_api_content(visible, recall, plugin)
        assert sent[0][0] == wire
        assert "model" in sent[0][1] and "reasoning_effort" in sent[0][1] and "service_tier" in sent[0][1]
        rows = db.get_messages(agent.session_id)
        assert [(r["role"], r["content"]) for r in rows] == [("user", visible), ("assistant", "done")]
        assert rows[0]["api_content"] == wire
        reloaded = db.get_messages_as_conversation(agent.session_id)
        assert reloaded[0]["content"] == visible
        seed = render_history_seed(reloaded + [{"role": "user", "content": "next"}])
        assert recall in seed and plugin in seed
    finally:
        agent._memory_manager = None
        db.close()
