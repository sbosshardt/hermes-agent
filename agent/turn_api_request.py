"""Per-attempt request assembly for the conversation turn's retry loop: re-apply the
reasoning echo pad and prompt-cache decoration for the CURRENT provider (a fallback may
differ from the primary), build ``api_kwargs``, run the surrogate/ASCII chokepoints, Codex
preflight, OpenRouter cache bypass, Copilot ``x-initiator``, the LLM request middleware,
the ``pre_api_request`` hook and the debug dump. Nothing here imports
``agent.conversation_loop`` at module level.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from agent.message_sanitization import sanitize_outbound_kwargs, strip_images_for_rejecting_model
from hermes_cli.observability.shared_metrics_efficiency import observe_request_tools
from utils import env_var_enabled

logger = logging.getLogger("agent.conversation_loop")


@dataclass
class ApiRequestBuild:
    """Always ``action == "fallthrough"``; the fields are the request-local values the caller
    rebinds for the attempt."""

    action: str
    api_messages: Any
    _moa_prepared_request: Any
    tools_for_api: Any
    api_kwargs: Any
    _original_api_kwargs: Any
    _llm_middleware_trace: Any


def _set_extra_header(api_kwargs: Any, key: str, value: str) -> None:
    """Copy-on-write header set (the dict may be shared with the transport's defaults)."""
    _xh = dict(api_kwargs.get("extra_headers") or {})
    _xh[key] = value
    api_kwargs["extra_headers"] = _xh


def _known_recall_text(content: Any) -> Any:
    """Recognize string content and only Hermes's one/two cache-text-part layout."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or not 1 <= len(content) <= 2:
        return None
    pieces = []
    for part in content:
        if (not isinstance(part, dict) or part.get("type") != "text"
                or set(part) - {"type", "text", "cache_control"}
                or not isinstance(part.get("text"), str)):
            return None
        if "cache_control" in part and part["cache_control"] not in (
            {"type": "ephemeral"}, {"type": "ephemeral", "ttl": "1h"},
        ):
            return None
        pieces.append(part["text"])
    return "".join(pieces)


def _recall_row_matches(rows: Any, provenance: Any) -> bool:
    from agent.turn_context import _AUTO_RECALL_CURRENT_KEY, _AUTO_RECALL_PRIVATE_KEYS

    if not isinstance(rows, list) or not 0 <= provenance.position < len(rows):
        return False
    marked = [row for row in rows if isinstance(row, dict)
              and any(key in row for key in _AUTO_RECALL_PRIVATE_KEYS)]
    row = rows[provenance.position]
    if len(marked) != 1 or marked[0] is not row or not isinstance(row, dict):
        return False
    marker = row.get(_AUTO_RECALL_CURRENT_KEY)
    return bool(type(marker) is str and marker == provenance.marker
                and row.get("role") == "user"
                and not any(key in row for key in _AUTO_RECALL_PRIVATE_KEYS
                            if key != _AUTO_RECALL_CURRENT_KEY)
                and _known_recall_text(row.get("content")) in (
                    provenance.expected_content, provenance.expected_content.strip(),
                ))


def _selected_recall_position(agent: Any, api_messages: Any) -> Any:
    """Validate the original fresh-row coordinate after context selection, not text search."""
    from agent.turn_context import _ChatAutoRecallProvenance

    provenance = getattr(agent, "_chat_auto_recall_provenance", None)
    if (type(provenance) is not _ChatAutoRecallProvenance
            or type(provenance.marker) is not str or not provenance.marker
            or type(provenance.position) is not int
            or not isinstance(provenance.expected_content, str)
            or not provenance.expected_content.strip()
            or not isinstance(provenance.selected_context, str)
            or provenance.selected_context != getattr(agent, "_auto_recall_context", None)
            or provenance.observation is not getattr(agent, "_last_auto_recall_observation", None)):
        return None
    return provenance if _recall_row_matches(api_messages, provenance) else None


def _observe_chat_recall_request(agent: Any, api_kwargs: Any, provenance: Any) -> None:
    """Observe assembled chat bytes only; selection success is not transport acceptance."""
    observation = getattr(agent, "_last_auto_recall_observation", None)
    selected = getattr(agent, "_auto_recall_context", None)
    if (getattr(agent, "api_mode", None) != "chat_completions"
            or not isinstance(observation, dict) or observation.get("append_logged")
            or observation.get("mode") != "prefetch"
            or observation.get("attempted") is not True
            or observation.get("success") is not True
            or not isinstance(selected, str) or not selected.strip()):
        return
    rows = api_kwargs.get("messages") if isinstance(api_kwargs, dict) else None
    appended = bool(provenance is not None
                    and provenance is getattr(agent, "_chat_auto_recall_provenance", None)
                    and provenance.observation is observation
                    and provenance.selected_context == selected
                    and _recall_row_matches(rows, provenance))
    observation["append_logged"] = True
    observation["append_stage"] = "assembled_provider_bound"
    observation["memory_context_appended"] = appended
    if appended and isinstance(rows, list) and provenance is not None:
        observation["memory_context_chars"] = len(_known_recall_text(rows[provenance.position]["content"]))
        observation.pop("append_failure_reason", None)
    else:
        observation["append_failure_reason"] = "not_in_final_current_user"
    logger.info(
        "Auto-recall chat assembly: appended=%s stage=assembled_provider_bound reason=%s chars=%d",
        appended, "none" if appended else "not_in_final_current_user",
        observation.get("memory_context_chars", 0) if appended else 0,
    )


def _strip_recall_provenance(*payloads: Any) -> None:
    """Strip private row fields without rewriting content or durable history."""
    from agent.turn_context import _AUTO_RECALL_PRIVATE_KEYS

    for payload in payloads:
        if isinstance(payload, dict):
            for key in _AUTO_RECALL_PRIVATE_KEYS:
                payload.pop(key, None)
        lists = (payload,) if isinstance(payload, list) else (
            (payload.get("messages"), payload.get("input")) if isinstance(payload, dict) else ()
        )
        for rows in lists:
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        for key in _AUTO_RECALL_PRIVATE_KEYS:
                            row.pop(key, None)


def _release_chat_recall_cleanup(agent: Any, *payloads: Any) -> None:
    """Retire transient wire-copy bindings after validation, never durable history."""
    retained = getattr(agent, "_chat_auto_recall_cleanup_payloads", ())
    agent._chat_auto_recall_cleanup_payloads = None
    agent._chat_auto_recall_provenance = None
    _strip_recall_provenance(
        *(retained if isinstance(retained, tuple) else ()), *payloads,
    )


def _recall_clean_hook_history(messages: Any) -> Any:
    """Untrusted historical markers must not leak through the hook's history projection."""
    from agent.turn_context import _AUTO_RECALL_PRIVATE_KEYS

    if not isinstance(messages, list) or not any(
        isinstance(row, dict) and any(key in row for key in _AUTO_RECALL_PRIVATE_KEYS)
        for row in messages
    ):
        return messages
    clean = [dict(row) if isinstance(row, dict) else row for row in messages]
    _strip_recall_provenance(clean)
    return clean


def _fire_pre_api_request_hook(
    agent: Any, api_kwargs: Any, api_messages: Any, _llm_middleware_trace: Any, *, messages: Any,
    original_user_message: Any, approx_tokens: Any, total_chars: Any, retry_count: Any,
    api_call_count: Any, api_request_id: Any, api_start_time: Any, effective_task_id: Any,
    turn_id: Any,
) -> None:
    from agent.conversation_loop import _system_prompt_for_hooks

    try:
        from hermes_cli.lifecycle import has_hook, invoke_hook as _invoke_hook
        if has_hook("pre_api_request"):
            request_messages = api_kwargs.get("messages")
            if not isinstance(request_messages, list):
                request_messages = api_kwargs.get("input")
            if not isinstance(request_messages, list):
                request_messages = api_messages
            # Shallow copies: plugins may retain the lists; deepcopy is costly.
            # ``request_messages``/``conversation_history`` are raw langfuse passthroughs.
            # Anthropic (``system``) and Responses/Codex (``instructions``) move the system
            # prompt out of messages; pass it for observability.
            _invoke_hook(
                "pre_api_request",
                task_id=effective_task_id,
                turn_id=turn_id,
                api_request_id=api_request_id,
                session_id=agent.session_id or "",
                user_message=original_user_message,
                conversation_history=list(messages),
                platform=agent.platform or "",
                model=agent.model,
                provider=agent.provider,
                base_url=agent.base_url,
                api_mode=agent.api_mode,
                api_call_count=api_call_count,
                retry_count=retry_count,
                request_messages=list(request_messages) if isinstance(request_messages, list) else [],
                system_prompt=_system_prompt_for_hooks(api_kwargs, request_messages),
                message_count=len(api_messages),
                tool_count=len(agent.tools or []),
                approx_input_tokens=approx_tokens,
                request_char_count=total_chars,
                max_tokens=agent.max_tokens,
                started_at=api_start_time,
                middleware_trace=list(_llm_middleware_trace),
                request=agent._api_request_payload_for_hook(api_kwargs),
            )
    except Exception:
        pass


def build_api_request(
    agent: Any, *, api_messages: Any, _moa_prepared_request: Any, tools_for_api: Any,
    system_message: Any, messages: Any, original_user_message: Any, approx_tokens: Any,
    total_chars: Any, retry_count: Any, api_call_count: Any, api_request_id: Any,
    api_start_time: Any, effective_task_id: Any, turn_id: Any,
) -> ApiRequestBuild:
    """Assemble the attempt's request in the original order (every mutation happens BEFORE
    middleware/hooks/debug dumps observe the payload)."""
    from agent.conversation_loop import (
        _moa_client_consumes_prepared_request, _redecorate_prompt_cache_for_provider,
    )

    # These references are cleanup-only, not evidence of current-row origin.
    recall_cleanup_payloads = [api_messages, _moa_prepared_request]
    api_kwargs: Any = None
    _original_api_kwargs: Any = None
    _llm_middleware_trace: Any = None
    recall_provenance = None
    try:
        agent._reset_stream_delivery_tracking()
        # Per-attempt first-chunk timestamp so a stale value never leaks into post_api_request.
        agent._last_api_first_chunk_at = None
        recall_provenance = _selected_recall_position(agent, api_messages)
        # api_messages was built for the primary; a fallback (DeepSeek / Kimi / MiMo) may
        # require reasoning_content — re-apply the echo-back pad (idempotent) and re-render
        # the prompt-cache decoration for the current provider.
        agent._reapply_reasoning_echo_for_provider(api_messages)
        api_messages, _moa_prepared_request, tools_for_api = (
            _redecorate_prompt_cache_for_provider(
                agent, api_messages, system_message=system_message, moa_prepared=_moa_prepared_request,
                tools_for_api=tools_for_api,
            )
        )
        recall_cleanup_payloads.extend((api_messages, _moa_prepared_request))
        # A model that rejected image content gets text only; history keeps the images.
        strip_images_for_rejecting_model(agent, api_messages)
        observe_request_tools(agent, tools_for_api)
        if tools_for_api == agent.tools:
            api_kwargs = agent._build_api_kwargs(api_messages)
        else:
            api_kwargs = agent._build_api_kwargs(api_messages, tools_for_api=tools_for_api)
        # Retain native kwargs even if preflight or middleware returns a distinct copy.
        recall_cleanup_payloads.append(api_kwargs)
        # Messages were scrubbed above; this walk covers the rest of the payload (tool descriptions,
        # extra_body, kwargs strings) — see sanitize_outbound_kwargs for the #50959 rationale.
        sanitize_outbound_kwargs(agent, api_kwargs)
        if agent.api_mode == "codex_responses":
            api_kwargs = agent._get_transport().preflight_kwargs(
                api_kwargs, allow_stream=False, is_github_responses=agent._is_copilot_url(),
                sanitize_harmony_tokens=agent._is_codex_backend(),
            )
        recall_cleanup_payloads.append(api_kwargs)
        # OpenRouter caching replays identical responses, even empty ones; an empty-response
        # retry must bypass the cache.
        if agent._empty_content_retries > 0 and agent._is_openrouter_url():
            _set_extra_header(api_kwargs, "X-OpenRouter-Cache", "false")
        # Copilot x-initiator: first call of a user turn is "user" (billed premium);
        # tool-loop follow-ups keep the default "agent".
        if getattr(agent, "_is_user_initiated_turn", False) and agent._is_copilot_url():
            _set_extra_header(api_kwargs, "x-initiator", "user")
            agent._is_user_initiated_turn = False
        try:
            from hermes_cli.middleware import apply_llm_request_middleware

            _llm_request_mw = apply_llm_request_middleware(
                api_kwargs, task_id=effective_task_id, turn_id=turn_id, api_request_id=api_request_id,
                session_id=agent.session_id or "", platform=agent.platform or "", model=agent.model,
                provider=agent.provider, base_url=agent.base_url, api_mode=agent.api_mode,
                api_call_count=api_call_count,
            )
            api_kwargs = _llm_request_mw.payload
            _original_api_kwargs = _llm_request_mw.original_payload
            _llm_middleware_trace = _llm_request_mw.trace
        except Exception:
            _original_api_kwargs = dict(api_kwargs)
            _llm_middleware_trace = []

        _observe_chat_recall_request(agent, api_kwargs, recall_provenance)
    finally:
        # Final-origin validation precedes stripping. Release on failure as well,
        # before hooks/debug/return can expose any known request copy.
        _release_chat_recall_cleanup(
            agent, *recall_cleanup_payloads, api_kwargs, _original_api_kwargs, _llm_middleware_trace,
        )
        recall_cleanup_payloads.clear()
        recall_provenance = None

    _fire_pre_api_request_hook(
        agent, api_kwargs, api_messages, _llm_middleware_trace,
        messages=_recall_clean_hook_history(messages),
        original_user_message=original_user_message, approx_tokens=approx_tokens,
        total_chars=total_chars, retry_count=retry_count, api_call_count=api_call_count,
        api_request_id=api_request_id, api_start_time=api_start_time,
        effective_task_id=effective_task_id, turn_id=turn_id,
    )

    if env_var_enabled("HERMES_DUMP_REQUESTS"):
        agent._dump_api_request_debug(api_kwargs, reason="preflight")

    # Private to the in-process MoA facade; added after middleware/hooks/debug dumps so
    # none serializes it into the provider payload. Re-read the live client:
    # rotation/fallback/cleanup rebuild agent.client between attempts; a native OpenAI
    # client rejects this key (TypeError).
    if _moa_prepared_request is not None and agent.provider == "moa":
        if _moa_client_consumes_prepared_request(agent.client):
            api_kwargs["_moa_prepared_request"] = _moa_prepared_request
        else:
            logger.warning(
                "MoA client replaced mid-turn (client=%s); sending the "
                "prepared prompt without the MoA handshake",
                type(agent.client).__name__,
            )
    return ApiRequestBuild(
        "fallthrough", api_messages, _moa_prepared_request, tools_for_api, api_kwargs,
        _original_api_kwargs, _llm_middleware_trace,
    )
