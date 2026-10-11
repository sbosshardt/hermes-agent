# Execreations Hermes Agent — maintained patch inventory

## Canonical branch contract

- `execreations` is the complete accepted runtime/test portfolio, both locally and in `sbosshardt/hermes-agent`. Publish reviewed accepted commits to the same branch and verify exact SHA equality; dated `backup/*` branches are historical snapshots, not the current deployment line.
- Runtime baseline remains upstream stable `v2026.9.21` (Hermes Agent `v0.21.4`). Synchronizing the fork does not adopt unreleased upstream `main`, reinstall dependencies or restart services.
- This public ledger contains source-level rationale and retirement criteria. Private deployment logs, session evidence and original unpublished operational history are retained separately, not added to public Git history.
- The canonical-sync candidate uses the already-published `9cd29ea5990d3a900fe148959937aaa09a10ef18` tree. All tracked runtime/test files and modes match the previously maintained checkout. Only these two public patch-ledger documents change.

## Accepted release-port carries

The following source-level units were accepted on the maintained release branch. Provenance identifiers identify the release-port commits; cherry-picked implementations need not have the same ancestry as historical topic branches.

| Candidate commit(s) | Area / files | Still-unique invariant | Retirement condition |
|---|---|---|---|
| `8c3e57f805`, `e9acc954f0` | `agent/title_generator.py`, title tests | Reject model chatter as titles; for typed `/topic` titles, derive a concise user-topic fallback rather than a conversational opener. | A release validates both prompt-echo/greeting and typed-topic fallback paths. |
| `fc197a3b43` | `agent/memory_manager.py`, memory tests | Recalled text is informational background, not an authoritative instruction source. | Upstream emits equivalently safe framing at all API insertion points. |
| `ff27ce0345` | `agent/turn_context.py`, context tests | Malformed optional memory/plugin sidecars cannot abort a turn. | Upstream normalizes optional context before both standard and alternate transports. |
| `8c9212ad83` | `agent/memory_manager.py`, async-sync tests | Prefetch work cannot starve behind a slow `sync_all()` write executor. | Upstream separates execution pools or removes the starvation path with a regression. |
| `2a0908092f`, `dbafcdb494`, `96e8bf5745` | Hindsight provider and turn-context/session-boundary tests | Preserve a configurable bounded prefetch join, discard late publication after a session switch, and fence all inline fallback/turn-start reads. | Upstream provides the same bounded, session-safe behavior at every call site. |
| `d17c7e4473`, `eba6a737e9`, `925af3e648`, `b7f046c506`, `a96670ba12`, `5e679c8422`, `14990275e3` | Codex runtime, lower-trust history seeding, guarded state-message sidecars, turn-context/session persistence, transport tests | Send selected-memory/plugin context only in API input, not persisted user text; recover only *acknowledged* wire input with row-bound provenance. Historical context stays lower-trust, while sanitized replay, clean retry, late-first-persist and real in-place compaction retain exact accepted wire or fail closed on stale rows. A late first DB flush must not infer a current Codex user's wire sidecar from raw/clean divergence after an accepted clean retry. | Upstream provides equivalent parity and thread recovery without privileged historical instructions, pre-ACK claims, fabricated sidecars, or replay/compaction loss. |
| `9c06532f81`, `fcacf7ec3a`, `a392afe997`, `9bbffd7dae`, `599cc25849`, `80284fd586`, `23c4966049` | `agent/turn_context.py`, `agent/turn_api_request.py`, `agent/codex_runtime.py`, `hermes_state.py`, `hermes_state_usage.py`, `agent/insights.py`, schema and tests | Per-session auto-recall attempt/success/failure/latency counters, bounded best-effort persistence under SQLite contention, and legacy read-only Insights compatibility. Non-durable chat observation checks a uniquely marked fresh current request after middleware, proving **assembly only**; Codex observation requires a verifiable ACK and exact fresh recall-bearing submitted suffix, with a verified pending history prefix. Integration test `4c9f68c963` exercises real prefetch state into Codex positive/negative/seed paths. No durable append counter. | Upstream provides equivalent bounded state/Insights metrics and accurately placed append observation across transports. |
| `82066532f0`, `ff00765fd3` (test-only) | `tests/hermes_state/` writer-lock audit helper, contract mutations, owner-thread runtime tests | Recognize only `SessionDB._execute_write`'s exact bounded-lock context after verifying the in-class acquire/yield/finally-release shape; reject unsafe changed arms, shadowed helpers, and deferred function/lambda writer uses. Actual temp-DB writes hold the current thread's mutex through callback, execute, commit and rollback, and contention refuses writer access. This is a scoped lexical tripwire, not a claim about arbitrary aliases or rebinding. | Upstream audits correctly recognize equivalent bounded writer locks and retain mutation plus owner-thread runtime coverage. |
| `848a5e3d90` | `plugins/memory/hindsight/embedded.py`, provider/probe tests | Recognize the lightweight `hindsight-embed==0.9.2` wrapper/`uvx` launcher instead of requiring heavy host-stack imports. | Upstream probes the installed isolated launcher API and health without false negatives. |
| `ee748995dd`, `022dcd1b37`, `92f7a4979a`, `3c3b0b9596` | Email adapter, email tests, `pyproject.toml`, `uv.lock` | Fail-closed trusted/aligned DKIM DNS `temperror` retry across disconnects/restarts with mailbox/profile-scoped durable state and explicit IMAP acknowledgement before dispatch; pin/release-exempt `dkimpy==1.1.8`. | Upstream covers the complete trusted gating, transient-DNS handling, acknowledgement, state scope, and dependency/rollback tests. |

## Accepted post-release carries

### Unattended Bitwarden browser vault and saved-origin OTP refusal

- Published source: `c9a22014c0496e225dcb48bc935238faac369d51`.
- Files: `agent/vault_backends/base.py`, `agent/vault_backends/bitwarden.py`, `tools/browser_vault_tool.py`, `hermes_cli/config_defaults.py`, vault tests and credential-vault documentation.
- Behavior: explicit unattended opt-in, isolated CLI appdata, expected-account binding and freshness checks; saved OTP resolution requires an exact allowed page origin before code generation. Credentials never become model-visible values.
- Classification: unattended unlock is an opt-in feature; saved-origin OTP refusal is a separate security fix. Historical overlap references include upstream #108316, #108930, #111143, #119682 and #119691; recheck current code/status before proposing anything upstream. The last detailed vault-overlap audit was 2026-09-25, not a current merge-status assertion.
- Conflict risk: high in backend, browser-vault and configuration surfaces. Retirement requires released parity for every sub-behavior, not just one overlapping PR.

### Reset the stale-stream breaker at each fresh user turn

- Published source: `9cd29ea5990d3a900fe148959937aaa09a10ef18`.
- Files: `agent/conversation_loop.py`, `tests/agent/test_stream_stale_breaker_reset.py`.
- Behavior: call the existing `_reset_stale_streak` before the user-turn API guard. The next unchanged-provider user message can retry; repeated stalls inside the current turn remain bounded. No `/model` command is needed.
- Historical overlap pointers: upstream #89587, #89804 and #89612. Half-open recovery is not automatically equivalent to a per-user-turn reset; validate exact behavior before retirement or proposing a PR.
- Conflict risk: medium at conversation-loop entry and stale-breaker tests. Retire only when the chosen stable release satisfies both fresh-turn recovery and within-turn give-up.
- Native Cursor event translation remains in the separate Cursor-provider plugin, not core or the installed SDK.

## Retired or split-trimmed historical units

- The prior synchronous current-turn recall opt-in is retired for the maintained deployment configuration, not claimed upstream-equivalent. Reassess before enabling that option.
- Old ambient-base-URL isolation was trimmed where the selected release supplies profile-secret scoping; retain the launcher probe above and do not restore a global import monkeypatch.
- Old Hindsight join and email composite commits were adapted to the release's new architecture. Do not stack their superseded full commits on top of the current port.

## Verification scope

The prior focused stale-reset gate passed 74 tests; the vault gate passed 94. Canonical-branch synchronization additionally checks source/blob/mode identity and reruns the focused stale-reset suite. This is not a new full-suite pass or a fresh runtime rollout. Preserve historical broad-suite baseline comparisons and private rollout receipts separately.

## Maintenance gate

Use stable release tags for runtime upgrades. For each carry, compare implementation, defaults, call sites and tests; classify keep, retire, rewrite or split-trim. Keep public source history clean, preserve rollback references, and obtain separate authorization before posting upstream issues/PRs/comments.
