# Execreations Hermes Agent — upstream contribution plan

`execreations` is the accepted patched deployment line and must match the fork's same branch. See `LOCAL_PATCH_INVENTORY.md` for all maintained behavior units. This branch is based on a stable release; it is not automatically suitable as a fresh-main PR base.

## Contribution boundaries

- Fork backup/synchronization does not authorize upstream issues, PRs or comments.
- Recheck current upstream main, the chosen stable tag, existing issue/PR chains and exact diffs before proposing a carry. Historical pointers are leads, not current merge-status evidence.
- Prepare each independently useful contribution on a clean upstream-main worktree; preserve accepted deployment code until released semantic parity is proven.
- Keep private operational/session evidence outside public repository history. Public ledgers record source rationale, test scope and retirement criteria only.

## Units to assess independently

- Title quality: chatter rejection and concise typed-topic fallback.
- Memory trust framing: recalled content is informational, not instructions.
- Optional-context robustness: malformed sidecars do not abort a turn.
- Prefetch scheduling and session fencing: separate slow-write starvation from bounded/session-safe publication.
- Recall telemetry and Insights: bounded persistence and accurate assembled-versus-acknowledged observations; never claim provider acceptance from prompt assembly alone.
- Codex context parity/recovery: API-only injected context, lower-trust history and provenance for acknowledged wire sidecars, including clean retries, delayed first persistence and in-place compaction.
- SQLite writer-lock audit: narrow tests-only contribution with contract mutations and actual owner-thread runtime coverage; not proof against arbitrary aliases/rebinding.
- Embedded Hindsight launcher: compatibility with the lightweight isolated launcher without ambient credential leakage.
- Email DKIM DNS retry: fail-closed trusted/aligned gating, durable mailbox/profile scope, explicit acknowledgement and dependency coverage.
- Unattended Bitwarden: split the opt-in headless feature from overlapping saved-origin OTP security fixes. Historically #119691/#119682 supplied stronger OTP-tab selection; recheck before adding a competing proposal.
- Fresh-turn stale-breaker reset: compare related #89587/#89804/#89612 against both required policies before a narrow follow-up. Keep Cursor-native event conversion in the separate plugin.

## Release and publication discipline

Port only still-needed behavior onto an approved stable release, keep tests and runtime source consistent, and validate before any deployment. Runtime upgrades and gateway activation are separate from this branch synchronization. Publish reviewed accepted source to `sbosshardt/execreations`, then read back its exact SHA and confirm local divergence is zero.
