"""Request-local proof of selected memory in a standard Chat Completions payload.

The marker is an ephemeral routing coordinate, not provider input or transcript data.
No content/text lookup can authenticate a historical row as this turn's row.
"""
from dataclasses import dataclass
from uuid import uuid4

_MARKER = "_auto_recall_current_turn_provenance"
_PRIVATE_PREFIXES = ("_auto_recall_", "_chat_auto_recall_")


class _OriginMarker(str):
    """Track deep-copied middleware rows that its result does not return.

    Middleware makes intermediate deep copies hidden from the request builder; the
    value is deliberately immutable on the wire, but deepcopy's memo names the
    new owner row so cleanup can scrub even a plugin-retained intermediate.
    """
    agent: object
    owner_ids: set[int]

    def __new__(cls, value, agent):
        obj = str.__new__(cls, value)
        obj.agent = agent
        obj.owner_ids = set()
        return obj

    def __deepcopy__(self, memo):
        cloned_owner = next((memo[owner] for owner in self.owner_ids if owner in memo), None)
        if isinstance(cloned_owner, dict):
            register_payload(self.agent, cloned_owner)
            self.owner_ids.add(id(cloned_owner))  # the copy receives this value on return
        return self


@dataclass(frozen=True)
class ChatAutoRecallProvenance:
    marker: str
    position: int
    observation: object
    selected: str
    content: str


def clear_private(value, seen=None):
    """Scrub registered request-owned copies, including detached rows, in place."""
    if seen is None:
        seen = set()
    if not isinstance(value, (dict, list, tuple)) or id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, dict):
        for key in list(value):
            if isinstance(key, str) and key.startswith(_PRIVATE_PREFIXES):
                value.pop(key, None)
            else:
                clear_private(value[key], seen)
    else:
        for item in value:
            clear_private(item, seen)


def register_payload(agent, payload):
    if getattr(agent, "_chat_auto_recall_cleanup_payloads", None) is not None:
        agent._chat_auto_recall_cleanup_payloads.append(payload)
        def mark(value):
            if isinstance(value, dict):
                token = value.get(_MARKER)
                if isinstance(token, _OriginMarker):
                    token.owner_ids.add(id(value))
                for item in value.values():
                    if isinstance(item, (dict, list, tuple)):
                        mark(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, (dict, list, tuple)):
                        mark(item)
        mark(payload)


def cleanup(agent, *payloads):
    known = getattr(agent, "_chat_auto_recall_cleanup_payloads", None) or ()
    for payload in (*known, *payloads):
        clear_private(payload)
    agent._chat_auto_recall_cleanup_payloads = None
    agent._chat_auto_recall_provenance = None


def bind(agent, rows, position, *, selected, composed):
    """Mark only the copied, freshly composed current user row (never history)."""
    cleanup(agent)
    agent._chat_auto_recall_cleanup_payloads = [rows]
    observation = getattr(agent, "_last_auto_recall_observation", None)
    from agent.memory_context import sanitize_context
    if (getattr(agent, "api_mode", None) != "chat_completions"
            or not isinstance(selected, str) or not sanitize_context(selected).strip()
            or not isinstance(observation, dict) or not observation.get("success")
            or selected != getattr(agent, "_auto_recall_context", None)
            or not isinstance(position, int) or isinstance(position, bool)
            or not isinstance(rows, list) or position < 0 or position >= len(rows)
            or not isinstance(composed, str) or not composed
            or not isinstance(rows[position], dict) or rows[position].get("role") != "user"
            or rows[position].get("content") != composed):
        agent._chat_auto_recall_provenance = None
        return
    marker = _OriginMarker(uuid4().hex, agent)
    rows[position][_MARKER] = marker
    register_payload(agent, rows[position])
    agent._chat_auto_recall_provenance = ChatAutoRecallProvenance(
        marker, position, observation, selected, composed,
    )


def _content_matches(value, expected):
    if isinstance(value, str):
        return value == expected
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        return False
    chunks = []
    for part in value:
        if not isinstance(part, dict) or set(part) - {"type", "text", "cache_control"}:
            return False
        if part.get("type") != "text" or not isinstance(part.get("text"), str):
            return False
        if "cache_control" in part and part["cache_control"] != {"type": "ephemeral"}:
            return False
        chunks.append(part["text"])
    return "".join(chunks) == expected


def observe_assembled(agent, kwargs):
    """Assembly only, not an acknowledgement that a provider accepted the request."""
    observation = getattr(agent, "_last_auto_recall_observation", None)
    proof = getattr(agent, "_chat_auto_recall_provenance", None)
    if not isinstance(observation, dict) or not observation.get("attempted"):
        return
    if getattr(agent, "api_mode", None) != "chat_completions":
        return
    rows = kwargs.get("messages") if isinstance(kwargs, dict) else None
    valid = (
        isinstance(proof, ChatAutoRecallProvenance)
        and proof.observation is observation
        and proof.selected == getattr(agent, "_auto_recall_context", None)
        and isinstance(proof.marker, str) and bool(proof.marker)
        and isinstance(proof.position, int) and not isinstance(proof.position, bool)
        and isinstance(rows, list) and 0 <= proof.position < len(rows)
        and isinstance(rows[proof.position], dict)
        and rows[proof.position].get("role") == "user"
        and rows[proof.position].get(_MARKER) is proof.marker
        and _content_matches(rows[proof.position].get("content"), proof.content)
        and sum(isinstance(row, dict) and row.get(_MARKER) is proof.marker for row in rows) == 1
        and sum(isinstance(row, dict) and _MARKER in row for row in rows) == 1
    )
    observation.update(
        memory_context_appended=bool(valid), append_stage="assembled_provider_bound",
        append_logged=bool(valid),
    )
    if not valid:
        observation["append_failure_reason"] = "not_in_final_current_user"
