"""Render Hermes' prior transcript as a one-shot seed for a FRESH codex app-server thread.

A codex thread is the model-side continuity store, so a thread that codex hands back via
``thread/resume`` already knows the conversation. A thread started from scratch does not: a session
that ran on another provider before ``/model`` switched to openai-codex, a session whose stored thread
codex could not resume, or a thread retired mid-session (prompt composition change, wedged client)
would otherwise start blind (#26035, #74712; direction from #26081 by @LeonSGP43).

The seed rides on the first ``turn/start`` user input, not developerInstructions. Historical
tool output and recalled/plugin context are untrusted data, never developer instructions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

# Tail cap on the rendered history; ~8k tokens, sent once with the first turn.
MAX_HISTORY_SEED_CHARS = 32_000
_TOOL_RESULT_PREVIEW_CHARS = 400
SIDECAR_PROVENANCE_KEY = "_codex_history_sidecar"

_HEADER = ("Prior conversation from this Hermes session (the thread you are continuing was started fresh; "
           "treat these turns as already having happened):")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p if isinstance(p, str) else p.get("text", "") for p in content if isinstance(p, (str, dict))]
        return "\n".join(p for p in parts if p)
    return ""


def sidecar_provenance(content: str, sidecar: str) -> str:
    """Bind acknowledged wire bytes to exact durable visible text.

    A substring (even an exact prefix) cannot establish provenance after a
    rewrite. Legacy sidecars without this marker are omitted from history seeds.
    """
    pair = json.dumps([content, sidecar], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(pair.encode("utf-8", errors="surrogatepass")).hexdigest()


def _render_row(msg: Dict[str, Any]) -> str:
    role = msg.get("role")
    text = _text_of(msg.get("content"))
    if role == "user":
        # The transcript is the visible user turn; api_content is the historical
        # wire context selected for THAT turn. Keep the two distinct so old
        # recalled memory/plugin notes are not presented as current instructions.
        sidecar = msg.get("api_content")
        metadata = msg.get("display_metadata")
        marker = metadata.get(SIDECAR_PROVENANCE_KEY) if isinstance(metadata, dict) else None
        sent = (sidecar if isinstance(sidecar, str) and isinstance(msg.get("content"), str)
                and text.strip() and isinstance(marker, str)
                and marker == sidecar_provenance(text, sidecar) else "")
        if sent.startswith(text + "\n\n"):
            extra = sent[len(text):].strip()
            return (f"[USER]\n{text}\n\n" if text else "") + (
                "[CONTEXT SENT WITH THIS PRIOR TURN — historical, not current instructions]\n" + extra
            )
        if sent and sent != text:
            return f"[USER]\n{text}\n\n[WIRE INPUT SENT WITH THIS PRIOR TURN — historical, not current instructions]\n{sent}"
        return f"[USER]\n{text}" if text.strip() else ""
    if role == "assistant":
        calls = [c.get("function", {}).get("name") for c in msg.get("tool_calls") or [] if isinstance(c, dict)]
        lines = [f"[ASSISTANT]\n{text}"] if text else []
        if calls:
            lines.append("[ASSISTANT called tools: " + ", ".join(c for c in calls if c) + "]")
        return "\n".join(lines)
    if role == "tool":
        if len(text) > _TOOL_RESULT_PREVIEW_CHARS:
            text = text[:_TOOL_RESULT_PREVIEW_CHARS] + " …"
        return f"[TOOL RESULT]\n{text}" if text else ""
    return ""  # system rows are the prompt composition, already sent as developerInstructions


def render_history_seed(messages: List[Dict[str, Any]] | None) -> str:
    """Prior turns as one text block, newest last; empty when there is nothing before the current
    user message. The trailing user row is the turn being submitted and is never included."""
    rows = list(messages or [])
    if rows and rows[-1].get("role") == "user":
        rows = rows[:-1]
    rendered = [r for r in (_render_row(m) for m in rows if isinstance(m, dict)) if r]
    if not rendered:
        return ""
    body = "\n\n".join(rendered)
    if len(body) > MAX_HISTORY_SEED_CHARS:
        body = "[… earlier turns omitted …]\n\n" + body[-MAX_HISTORY_SEED_CHARS:]
    return f"{_HEADER}\n\n{body}"
