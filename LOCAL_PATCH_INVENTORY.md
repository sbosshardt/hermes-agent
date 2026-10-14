# Local v0.21.6 candidate patch inventory

## Release/source contract

Base: Hermes stable v0.21.6, peeled `818c13be1dc4fd28987e1e881a9408224afd4535`.
This is an isolated local candidate, not the maintained deployed branch or a published release. Core and extracted Hindsight plugin have separate source identities. Source/test grouping below describes remaining local behavior; it does not claim full readiness or current upstream issue status.

## Core-owned retained deltas

- **Title quality** — `agent/title_generator.py`, `test_title_topic_fallback.py`: reject conversational title chatter and derive the typed topic fallback. Retire upon released parity for both paths.
- **Fresh-user-turn retry** — `agent/conversation_loop.py`, `test_stream_stale_breaker_reset.py`: reset the existing stale-stream breaker at the next user turn without removing within-turn bounded give-up. Retire upon equivalent two-policy regression coverage.
- **Memory scheduling/session ownership** — `agent/memory_manager.py` and async boundary tests: keep prefetch independent from slow writes, FIFO session-boundary dependencies, construction/submission fallbacks and bounded shared abandonment/drain. Direct read/turn-start admission includes the pending boundary and lock admission in one deadline. Retire only when all entrypoints and failures have released parity.
- **Optional context and trust framing** — `agent/turn_context.py`, `agent/turn_api_request.py` and optional-context/observer tests: malformed optional memory cannot abort a turn; recalled content remains lower-trust background; verify observation at assembly/ACK boundaries rather than claiming server acceptance. Request-local provenance copies are cleaned while durable history survives. Retire after equivalent behavior and retained-copy regressions.
- **Codex context/ACK/recovery parity** — runtime, transport, history seeding and session persistence: carry injected context only in submitted API input; persist/replay accepted wire with exact row-bound provenance after a genuine nonblank string ACK. Recovered history is lower-trust fresh-user input, never privileged historical instructions. Reject malformed/stale bindings and clean retry divergence. Retire after equivalent native ACK/refusal/persistence/recovery coverage.
- **Bounded recall telemetry/state writes** — `hermes_state*.py`, turn context/request and runtime consumers: per-session metrics with bounded opt-in best-effort SQLite persistence, default critical writer behavior preserved. Test-only lexical audit recognizes the exact bounded writer context and real owner-thread lock use, not arbitrary aliases/rebinding. Retire after complete state/Insights and lock mutation/owner coverage.
- **Email DKIM transient DNS retry** — `plugins/platforms/email/adapter.py`, additive retry regressions, core metadata: preserve target sender/preauthorization/parser/authentication guards; retain mailbox/profile-scoped pending state and acknowledge IMAP before dispatch. Exact declared package is `dkimpy==1.1.8; python_version >= '3.14'`. Retire after complete trusted/aligned DNS retry, scope and acknowledgement parity.
- **Unattended Bitwarden and saved-origin OTP** — vault backend/base/browser-vault source and additive tests: opt-in owned password-file/account/CLI-appdata checks, freshness fencing and no second credential copy. Reject OTP resolution before secret retrieval on a mismatched saved origin; preserve newer native lease/navigation/origin controls. Retire feature and security components independently only upon released parity.

## Split/retired ownership

External Hindsight bounded join/generation publication is in its separately pinned plugin ledger, not reintroduced into core. Preserve upstream target source/test security changes instead of replaying old whole modules. Old embedded-provider import probing is not a host heavy-stack requirement for a lightweight isolated launcher. Historical deployed portfolio and upstream pointers remain outside this candidate's acceptance claim.

## Dependency/readiness limitation

The universal release lock has an unchanged optional Android/Playwright wheel resolution failure. It is retained unchanged, not falsely advertised as regenerated. A separately owned Linux/Python3.14/x86_64 lock resolves the exact DKIM closure for this host without modifying portable declarations. Final native PM-owned application/plugin generation and profile admission still require their own packaging evidence; hash-locked test setup alone is not deployment readiness.

## Publication policy

Keep this candidate local until independent scope/security/quality/operational gates. No remote writes, production activation, unrelated upstream bug fixes or version/route/policy changes are authorized by these ledgers.
