"""Vertical tracer: selected recall -> real turn -> exact ACK observation.

Only Codex inference is a recording peer. Exercise actual prologue, composition,
metrics, local SessionDB and runtime; no external SDK/provider acceptance claimed.
"""
import logging
import socket
from unittest.mock import MagicMock

from agent import codex_runtime, turn_context
from agent.transports.codex_app_server_session import TurnResult
from agent.turn_context import compose_user_api_content
from hermes_state import SessionDB
from run_agent import AIAgent


def test_selected_recall_exact_ack_is_observed_without_payload_logging(monkeypatch, tmp_path, caplog):
    def refuse_connect(*args, **kwargs):
        raise AssertionError("isolated observer tracer must not connect to a provider")

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    db = SessionDB(tmp_path / "state.db")
    visible = "What did we decide about the release deployment?"
    recall = "<selected-memory>use the reviewed staging candidate</selected-memory>"
    plugin = "<plugin-note>keep the production cutover held</plugin-note>"
    sent = []

    class RecordingSession:
        _history_seed_pending = False
        _history_seed = None

        def ensure_started(self):
            return "thread-recall-observation"

        def close(self):
            pass

        def run_turn(self, user_input, **kwargs):
            sent.append(user_input)
            return TurnResult(final_text="done", thread_id="thread-recall-observation",
                              turn_id="accepted-recall-observation", input_accepted=True,
                              submitted_user_text=user_input,
                              projected_messages=[{"role": "user", "content": user_input},
                                                  {"role": "assistant", "content": "done"}])

    agent = AIAgent(api_key="fixture-key", base_url="https://fixture.invalid", provider="openai",
                    api_mode="codex_app_server", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, session_db=db, session_id="isolated-recall-observation")
    agent._memory_manager = MagicMock()
    agent._memory_manager.build_system_prompt.return_value = ""
    agent._memory_manager.describe_recall.return_value = ""
    agent._memory_manager.wait_for_session_boundary.return_value = True
    agent._memory_manager.prefetch_all.return_value = recall
    monkeypatch.setattr(turn_context, "_maybe_title_session_at_turn_start", lambda *args: None)
    monkeypatch.setattr(turn_context, "_collect_pre_llm_call_context", lambda *args, **kwargs: plugin)
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session", lambda _agent, messages: None)
    agent._codex_session = RecordingSession()
    try:
        with caplog.at_level(logging.INFO, logger="agent.codex_runtime"):
            assert agent.run_conversation(visible)["completed"]
        wire = compose_user_api_content(visible, recall, plugin)
        assert sent == [wire]
        rows = db.get_messages(agent.session_id)
        assert [(row["role"], row["content"]) for row in rows] == [("user", visible), ("assistant", "done")]
        assert rows[0]["api_content"] == wire
        counters = db.get_session(agent.session_id)
        assert counters["auto_recall_attempt_count"] == counters["auto_recall_success_count"] == 1
        observation = agent._last_auto_recall_observation
        assert observation["attempted"] is True and observation["success"] is True
        assert observation.get("append_logged") is True
        assert observation["memory_context_appended"] is True
        assert observation["memory_context_chars"] == len(wire)
        assert recall not in str(observation) and plugin not in str(observation)
        append_logs = [record.getMessage() for record in caplog.records
                       if "memory-context appended" in record.getMessage()]
        assert len(append_logs) == 1
        assert all(recall not in message and plugin not in message for message in append_logs)
    finally:
        agent._memory_manager = None
        db.close()
