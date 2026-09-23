# Local Patch Inventory — Execreations Hermes Agent

## Release window (candidate, not yet deployed)

- **Baseline:** upstream stable `v2026.9.21` (Hermes Agent `v0.21.4`). The maintained `execreations` checkout and `sbosshardt/execreations` still point to `419ef2a84d` on this checkpoint; this candidate is an isolated branch/worktree.
- **Rollback refs:** `backup/pre-v2026.9.21-20260922T231333Z` and tag `backup-pre-v2026.9.21-20260922T231333Z` at `419ef2a84d`.
- **Initial verified backup:** `/home/hermes/.hermes/backups/v2026.9.21-20260922T231333Z/` (69 items, including online SQLite state backups). Refresh immediately before cutover: TowTargetDev `/topic` was enabled after that snapshot.
- **Execution state:** Samuel resumed release work after the VPS reboot. The candidate is not the running runtime, its final telemetry carry is awaiting a spec-review fix, and production branch/venv/services/fork have not moved. GitHub force-with-lease requires separate explicit approval.

Only intentional runtime deltas relative to `v2026.9.21` are listed below. Test-only adjustments belong to the behavior they validate.

## Candidate runtime carries

| Candidate commit(s) | Area / files | Still-unique invariant | Retirement condition |
|---|---|---|---|
| `8c3e57f805`, `e9acc954f0` | `agent/title_generator.py`, title tests | Reject model chatter as titles; for typed `/topic` titles, derive a concise user-topic fallback rather than a conversational opener. | A release validates both prompt-echo/greeting and typed-topic fallback paths. |
| `fc197a3b43` | `agent/memory_manager.py`, memory tests | Recalled text is informational background, not an authoritative instruction source. | Upstream emits equivalently safe framing at all API insertion points. |
| `ff27ce0345` | `agent/turn_context.py`, context tests | Malformed optional memory/plugin sidecars cannot abort a turn. | Upstream normalizes optional context before both standard and alternate transports. |
| `8c9212ad83` | `agent/memory_manager.py`, async-sync tests | Prefetch work cannot starve behind a slow `sync_all()` write executor. | Upstream separates execution pools or removes the starvation path with a regression. |
| `2a0908092f`, `dbafcdb494`, `96e8bf5745` | Hindsight provider and turn-context/session-boundary tests | Preserve a configurable bounded prefetch join, discard late publication after a session switch, and fence all inline fallback/turn-start reads. | Upstream provides the same bounded, session-safe behavior at every call site. |
| `d17c7e4473`, `eba6a737e9` | Codex runtime, history seeding, transport tests | Send selected-memory/plugin context only in the API payload, not persisted user text; recover the *actual sent* context on a fresh app-server thread. | Upstream has transport parity and thread-recovery coverage without leaking transient data to stored history. |
| `848a5e3d90` | `plugins/memory/hindsight/embedded.py`, provider/probe tests | Recognize the lightweight `hindsight-embed==0.9.2` wrapper/`uvx` launcher instead of requiring heavy host-stack imports. | Upstream probes the installed isolated launcher API and health without false negatives. |
| `ee748995dd`, `022dcd1b37`, `92f7a4979a`, `3c3b0b9596` | Email adapter, email tests, `pyproject.toml`, `uv.lock` | Fail-closed trusted/aligned DKIM DNS `temperror` retry across disconnects/restarts with mailbox/profile-scoped durable state and explicit IMAP acknowledgement before dispatch; pin/release-exempt `dkimpy==1.1.8`. | Upstream covers the complete trusted gating, transient-DNS handling, acknowledgement, state scope, and dependency/rollback tests. |

**Pending, not yet an active candidate carry:** the prior `5126d8cdbf` per-session auto-recall attempt/success/failure/latency and actual append telemetry, schema and Insights must be ported. Worker commit `16997986e2` passed focused tests but independent spec review found read-only legacy Insights and turn-stall defects. Do not integrate it until fixed and re-reviewed; update this ledger with the final landed SHA(s).

## Retired or trimmed from the prior deployment

- `3998d0747b` (provider-agnostic `memory.sync_recall` opt-in) is **retired for current config**, not claimed upstream-equivalent. Active inspected profiles have it false/unset; default, TowTargetDev, and EthereumCase use agent-driven Hindsight tools mode. Reassess before re-enabling it.
- The ambient-base-URL leak portion of `4e3e6690b6`/`77770c23db` is trimmed because the tag scopes secrets/profile config. The launcher-aware probe is retained in `848a5e3d90`; do not restore global `importlib` monkeypatches from `0700607347`.
- The old Hindsight join body from `69d5e21fc9` is split and adapted to the new provider/turn-context architecture; it was not blindly cherry-picked.
- Historical split email sources `9f08f13132`, `f76048bf52`, `c1259f38eb`, and `d9890f7503` remain retired. Replay neither their superseded commits nor the old composite source without the new release-tag adaptation.

## Validation gate (in progress)

- Post-reboot built-in live-system guard canary: **49 passed**. Optional external `pytest_live_guard.py` is absent, so the canonical wrapper must not explicitly import it.
- Intermediate focused gates before telemetry integration: title/email **145 passed**, memory/session boundary **123 passed**, Codex/Hindsight/email robustness **213 passed**. Separate memory-worker focus **220 passed** is not integrated-candidate evidence.
- The currently running Hindsight daemon `/health` is healthy; the isolated test venv imports `hindsight-embed==0.9.2` and `dkimpy==1.1.8`. Candidate dashboard assets exist. This is a pre-cutover observation, not post-restart proof.
- **Still required:** review/fix/integrate telemetry; freeze candidate; integrated focused gate; copied-state migration of 11 profile DBs with counts/integrity; canonical broad suite at nice +10/max six workers (its default discovery excludes Docker/E2E/external integration jobs, which are separate coverage); baseline reproduction of failures from clean tag import root; candidate config/CLI checks; refresh secure online backups; coordinate cutover/rollback, then verify all affected profiles and external services. Creative remains on its separate v2026.8.3 runtime and must not have its live DB migrated by the shared checkout update.
