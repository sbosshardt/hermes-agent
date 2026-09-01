# Local Patch Inventory — Execreations Hermes Agent

## Release window

- **Candidate baseline:** upstream `v2026.8.31` (Hermes Agent `v0.21.0`)
- **Candidate branch:** `candidate/v2026.8.31-20260831T201409Z`
- **Candidate status:** validated; production cutover pending
- **Rollback branch:** `backup/pre-v2026.8.31-20260831T201409Z`
- **Rollback tag:** `backup-pre-v2026.8.31-20260831T201409Z`
- **Profile/state backup:** `/home/hermes/.hermes/backups/v2026.8.31-20260831T201409Z/`

Only the runtime behavior listed below is intentionally carried. Compatibility-only test commits are grouped with the runtime carry they validate.

## Active runtime carries

| Candidate commit(s) | Source carry | Files | Why it remains local | Retirement condition |
|---|---|---|---|---|
| `dc2b4a4783` | `7492a81be1` | `agent/title_generator.py`, title tests | Reject conversational model-openers as session titles rather than displaying generic model chatter. | Upstream validates/replaces opener-shaped title output with regression coverage. |
| `69d5e21fc9` | `f4336819e4` | Hindsight provider and tests | Bound Hindsight prefetch joins and preserve retain/prefetch ordering. | Upstream provides equivalent bounds and ordering coverage. |
| `3998d0747b` | `86bc271cbe` | memory init/provider/manager, RetainDB, `run_agent.py` | Config-gated synchronous recall for current-turn relevance when a provider supports it. | Upstream supplies the same opt-in contract and tests. |
| `cfaf8782d2` | `2ea8624245` | memory manager and async-sync tests | Keep prefetch work off the sync executor so `sync_all` cannot be starved. | Upstream separates these execution pools or removes the starvation path. |
| `bf13c0d3c5` | `8cde1246d6` | memory manager and provider tests | Frame recalled memory as informational background rather than authoritative instructions. | Upstream adopts equivalent safe framing at the API insertion point. |
| `5126d8cdbf` | `2d93bbac2f` | turn loop/context, state, Insights, tests | Persist auto-recall attempts/appends and expose them through Insights. | Upstream offers equivalent state-compatible telemetry and tests. |
| `f4ce8d74de` | `2598db3f19` | Codex runtime/turn loop/forwarder and tests | Give Codex app-server turns the same selected-memory context as the standard transport without persisting injected bytes as user input. | Upstream establishes that transport parity. |
| `6054decb68` | `41e040c9cd` | turn context and telemetry tests | Ignore malformed optional context sidecars rather than aborting a turn. | Upstream validates optional sidecars safely. |
| `4e3e6690b6`, `77770c23db`, `566911de98` | `ea0359cd3a`, `eded92cda9` | Hindsight provider and tests | Prevent ambient LLM base-URL leakage into embedded Hindsight profiles and validate the `uvx`-isolated launcher rather than requiring heavy host imports. | Upstream isolates profile-env construction and performs launcher/API-compatible availability checks. |
| `372eb51152`, `c0fdec1f28`, `8c10360706` | `8755d0d061` / composite source `8937b537a4` | Email adapter, email tests, `pyproject.toml`, `uv.lock` | Safely retry trusted aligned DKIM `temperror`; persist mailbox-bound fail-closed state; acknowledge IMAP mail before dispatch; pin and release-exempt `dkimpy==1.1.8`. | Upstream includes the complete trusted-gating, retry, durable-state, acknowledgement, dependency, and regression-test behavior. |

## Retired history

- `0700607347` is **retired**. Its old ordering workaround conflicted with the new launcher-probe coverage; `566911de98` replaces it by avoiding a global `importlib` monkeypatch.
- The older split email sources `9f08f13132`, `f76048bf52`, and `c1259f38eb` remain superseded by the maintained composite source `8937b537a4`.
- `d9890f7503` remains retired; the relevant IMAP acknowledgement coverage is part of the maintained composite email carry.

## Validation evidence

- Candidate carried-patch suite: **578 passed, 2 skipped**. It covers title, memory, Hindsight, telemetry/state/Insights, Codex app-server, email/retry/robustness, and packaging metadata.
- Candidate Hindsight runtime: `hindsight-client`, `hindsight-embed==0.9.2`, and `DaemonEmbedManager` import successfully; `_check_local_runtime()` returns `(True, None)`.
- Candidate config dry run: current profile schema **38 → 39** migrates successfully in a copied `HERMES_HOME`.
- Canonical per-file suite ran at **nice +10, max 6 workers** using external test venvs so installer tests could not remove the runner's own `pytest` dependency.
- The candidate's remaining **96 failures in 28 files** all reproduce on a clean `v2026.8.31` baseline. The clean tag additionally fails `tests/agent/test_compression_attempt_lifecycle.py`; it passes in the candidate. These are release/environment baseline failures, not carried-patch regressions.
- Candidate-specific validation failures were repaired before this record: the email robustness mock now models the required `STORE \\Seen` acknowledgement, and `dkimpy` is exempted from the release `exclude-newer` cutoff with `uv.lock` refreshed.

## Cutover checklist

- [x] Rollback refs and profile/state backup created.
- [x] Candidate branch built from the release tag with only active carries.
- [x] Dependency and embedded-Hindsight runtime validation complete in isolation.
- [x] Candidate-focused tests pass.
- [x] Canonical-suite failures compared to a clean release-tag baseline.
- [ ] Move `execreations` to the validated candidate.
- [ ] Sync active runtime dependencies, install `hindsight-embed==0.9.2`, migrate config to schema 39.
- [ ] Push `execreations` to `sbosshardt/execreations` after explicit GitHub-write approval.
- [ ] Restart and verify gateway, Telegram, Email, API, and Hindsight.
