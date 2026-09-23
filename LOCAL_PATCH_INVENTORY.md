# Local Patch Inventory — Execreations Hermes Agent

## Release window (candidate, not yet deployed)

- **Baseline:** upstream stable `v2026.9.21` (Hermes Agent `v0.21.4`). The maintained `execreations` checkout and `sbosshardt/execreations` still point to `419ef2a84d` on this checkpoint; this candidate is an isolated branch/worktree.
- **Rollback refs:** `backup/pre-v2026.9.21-20260922T231333Z` and tag `backup-pre-v2026.9.21-20260922T231333Z` at `419ef2a84d`.
- **Initial verified backup:** `/home/hermes/.hermes/backups/v2026.9.21-20260922T231333Z/` (69 items, including online SQLite state backups). Refresh immediately before cutover: TowTargetDev `/topic` was enabled after that snapshot.
- **Execution state:** Candidate is not the running runtime; telemetry storage/Insights is integrated and Codex security plus actual append-observation accuracy remain blocked under independent review. Production branch/venv/services/fork have not moved. Samuel approved a fresh-lease guarded fork rewrite and a brief coordinated six-gateway outage followed by serial startup **only after** all validation and rollback gates pass; Creative remains separate.

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
| `9c06532f81`, `fcacf7ec3a`, `a392afe997` | `agent/turn_context.py`, `hermes_state.py`, `hermes_state_usage.py`, `agent/insights.py`, schema and tests | Per-session auto-recall attempt/success/failure/latency counters, best-effort bounded persistence under SQLite contention, and legacy read-only Insights compatibility. Append-observation logging is still under review: do not claim it proves provider acceptance. | Upstream provides equivalent bounded state/Insights metrics and accurately placed append observation across transports. |
| `848a5e3d90` | `plugins/memory/hindsight/embedded.py`, provider/probe tests | Recognize the lightweight `hindsight-embed==0.9.2` wrapper/`uvx` launcher instead of requiring heavy host-stack imports. | Upstream probes the installed isolated launcher API and health without false negatives. |
| `ee748995dd`, `022dcd1b37`, `92f7a4979a`, `3c3b0b9596` | Email adapter, email tests, `pyproject.toml`, `uv.lock` | Fail-closed trusted/aligned DKIM DNS `temperror` retry across disconnects/restarts with mailbox/profile-scoped durable state and explicit IMAP acknowledgement before dispatch; pin/release-exempt `dkimpy==1.1.8`. | Upstream covers the complete trusted gating, transient-DNS handling, acknowledgement, state scope, and dependency/rollback tests. |

**Pending before release:** the carried Codex thread-recovery implementation in `eba6a737e9` promotes historical untrusted data into developer instructions. Isolated repair `951e89c8f4` addresses the role boundary, but independent review found early unsent-sidecar and in-place-compaction backfill defects; its follow-up is not integrated. The auto-recall append log is still too early/permissive, especially for Codex acknowledgement and final chat request middleware; validate a narrowly scoped correction before cutover.

## Retired or trimmed from the prior deployment

- `3998d0747b` (provider-agnostic `memory.sync_recall` opt-in) is **retired for current config**, not claimed upstream-equivalent. Active inspected profiles have it false/unset; default, TowTargetDev, and EthereumCase use agent-driven Hindsight tools mode. Reassess before re-enabling it.
- The ambient-base-URL leak portion of `4e3e6690b6`/`77770c23db` is trimmed because the tag scopes secrets/profile config. The launcher-aware probe is retained in `848a5e3d90`; do not restore global `importlib` monkeypatches from `0700607347`.
- The old Hindsight join body from `69d5e21fc9` is split and adapted to the new provider/turn-context architecture; it was not blindly cherry-picked.
- Historical split email sources `9f08f13132`, `f76048bf52`, `c1259f38eb`, and `d9890f7503` remain retired. Replay neither their superseded commits nor the old composite source without the new release-tag adaptation.

## Validation gate (in progress)

- Post-reboot built-in live-system guard canary: **49 passed**. Optional external `pytest_live_guard.py` is absent, so the canonical wrapper must not explicitly import it.
- Intermediate gates: title/email **145 passed**, memory/session boundary **123 passed**, Codex/Hindsight/email robustness **213 passed**. After telemetry integration, candidate memory/Codex slice **185 passed** and relocated state suite **285 passed, 2 skipped**; the prior memory-worker focus **220 passed** is not integrated-candidate evidence. Codex isolated repair `951e89c8f4` separately passed **47 tests** but failed independent spec review.
- The currently running Hindsight daemon `/health` is healthy; the isolated test venv imports `hindsight-embed==0.9.2` and `dkimpy==1.1.8`. Candidate dashboard assets exist. This is a pre-cutover observation, not post-restart proof.
- Copy-only telemetry migration rehearsal: **11/11 backup DBs** kept session counts and integrity, with six telemetry columns; rerun against a fresh final snapshot. The isolated candidate/test interpreter links SQLite 3.50.4; active runtime links 3.53.1 and must retain a safe version after sync.
- **Still required:** fix/review/integrate Codex security and append observation; freeze candidate; final integrated focused gate and canonical broad suite at nice +10/max six workers (default discovery excludes Docker/E2E/external integration jobs); baseline reproduction of failures from clean tag import root; final copied-state migration and candidate config/CLI checks; refresh secure online backups; coordinate cutover/rollback, then verify all affected profiles and external services. Creative remains on its separate v2026.8.3 runtime and must not have its live DB migrated by the shared checkout update.
