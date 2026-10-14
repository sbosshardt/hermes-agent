"""Selected current context reaches Codex and acknowledged wire can be recovered.

Exercise actual AIAgent prologue, runtime, local SessionDB and history renderer.
The recording session peer replaces Codex inference only; no provider network.
"""
from unittest.mock import MagicMock

from agent import codex_runtime, turn_context
from agent.codex_runtime_history_seed import render_history_seed
from agent.transports.codex_app_server_session import TurnResult
from agent.turn_context import compose_user_api_content
from hermes_state import SessionDB
from run_agent import AIAgent


def test_selected_current_context_is_sent_and_recovers_without_dirtying_transcript(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    visible = "What did we decide about deployment?"
    recall = "<selected-memory>use the reviewed staging candidate</selected-memory>"
    plugin = "<plugin-note>keep the production cutover held</plugin-note>"
    sent, seeded = [], []

    class RecordingSession:
        _history_seed_pending = False
        _history_seed = None

        def ensure_started(self):
            return "thread-context"

        def close(self):
            pass

        def run_turn(self, user_input, **kwargs):
            sent.append(user_input)
            return TurnResult(final_text="done", thread_id="thread-context", turn_id="accepted-context",
                              input_accepted=True, submitted_user_text=user_input,
                              projected_messages=[{"role": "user", "content": user_input},
                                                  {"role": "assistant", "content": "done"}])

    agent = AIAgent(api_key="fixture-key", base_url="https://fixture.invalid", provider="openai",
                    api_mode="codex_app_server", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, session_db=db, session_id="isolated-context-recovery")
    agent._memory_manager = MagicMock()
    agent._memory_manager.build_system_prompt.return_value = ""
    agent._memory_manager.describe_recall.return_value = ""
    agent._memory_manager.prefetch_all.return_value = recall
    monkeypatch.setattr(turn_context, "_maybe_title_session_at_turn_start", lambda *args: None)
    monkeypatch.setattr(turn_context, "_collect_pre_llm_call_context", lambda *args, **kwargs: plugin)
    monkeypatch.setattr(codex_runtime, "_ensure_codex_session",
                        lambda _agent, messages: seeded.append([dict(row) for row in messages]))
    agent._codex_session = RecordingSession()
    try:
        assert agent.run_conversation(visible)["completed"]
        wire = compose_user_api_content(visible, recall, plugin)
        assert sent == [wire]
        assert seeded[0][-1]["content"] == visible
        rows = db.get_messages(agent.session_id)
        assert [(row["role"], row["content"]) for row in rows] == [("user", visible), ("assistant", "done")]
        assert rows[0]["api_content"] == wire
        reloaded = db.get_messages_as_conversation(agent.session_id)
        assert reloaded[0]["content"] == visible
        recovery = render_history_seed(reloaded + [{"role": "user", "content": "next question"}])
        assert recall in recovery and plugin in recovery
        assert len([row for row in rows if row["role"] == "user"]) == 1
    finally:
        agent._memory_manager = None
        db.close()
