# Local Patch Upstream Plan — Execreations Hermes Agent

## Baseline and release policy

- **Release candidate:** `v2026.9.21` / Hermes Agent `v0.21.4`, in an isolated worktree; not deployed or pushed as of this record.
- **Maintained branch:** `execreations`, still `419ef2a84d` on the previous release. Its fork branch is synchronized at that SHA; new candidate ancestry does not fast-forward from it.
- **Policy:** use upstream *stable tags*, port only still-needed behavior, validate with rollback in hand, then cut over serially. Movement on `origin/main` alone is not a new stable release.
- **GitHub authorization:** Samuel approved a guarded `--force-with-lease` update of `sbosshardt/execreations` **only after** all candidate validation and rollback gates pass, using a freshly re-read exact remote SHA. That approval does not authorize a public issue, PR, comment, or unrelated branch push; none has occurred.

## Candidate upstream contributions (proposals, not submissions)

| Local area | Recommended scope | Preserve/test before any PR |
|---|---|---|
| Title quality | Small independent PR against current upstream main. | Reject response-shaped/greeting titles and retain concise typed-topic fallback. |
| Memory framing | Independent prompt-injection-hardening PR. | Recalled material remains informational, including alternate transports. |
| Prefetch scheduling and session fence | Split into independent executor-starvation and join/session-boundary PRs if call sites remain separable. | No `sync_all()` starvation; bounded joins; no late or stale session publication; no turn-start inline leak. |
| Auto-recall state/Insights telemetry (candidate storage fix landed; append review pending) | State/Insights PR only after local append-observation correction and final review pass. | Additive schema; durable per-session attempt/success/failure/latency, read-only legacy Insights compatibility, best-effort bounded write. Observe actual final provider-bound assembly or Codex accepted turn, not a quoted memory block in an early copy; distinguish assembly from provider acceptance. |
| Codex app-server context parity and recovery (security repair pending) | Focused transport PR (may split API-content parity from thread-recovery continuity). | API-only injected memory/plugin context, original stored user text, lower-trust historical recovery (never developer instructions), sidecars only for acknowledged wire input with durable provenance, including clean overrides/compaction. |
| Embedded Hindsight launcher | Small provider-runtime PR. | `hindsight-embed` lightweight `uvx` wrapper works without heavy host imports and without ambient base-URL credential leakage. |
| Email aligned DKIM `temperror` retry | Security-focused PR with an explicit trust-boundary review. | Trusted sender and aligned auth only; retry transient DNS errors, durable mailbox/profile-scoped state across reconnect/restart, ack before dispatch, `dkimpy` packaging, no untrusted dispatch. |

## Tracking and overlap gate

- No issue/PR identifier in this document has been claimed as a current exact tracker. Before opening or commenting, inspect current `origin/main`, release tag, linked PR chains, and exact diffs; search for an active issue/PR that already owns the behavior.
- If an active PR includes the same hunk, do not duplicate it. If it addresses only an adjacent path, state the missing call site/default/test and provide a tested comparison before contributing. Do not post vague “we also see this” comments.
- Prepare each PR on a clean upstream-main worktree (not the deployed branch); use regression tests and evidence-backed PR body. Preserve local carries until a *released* upstream tag has the same semantics, defaults, and call-site/test coverage.
- Keep this plan and `LOCAL_PATCH_INVENTORY.md` synchronized whenever a carry lands, splits, or retires. The old synchronous current-turn recall carry is currently retired by deployment choice, not by verified upstream equivalence; re-audit if the option is restored.

## Historical email source note

Prior composite email source `8937b537a4` superseded split sources `9f08f13132`, `f76048bf52`, `c1259f38eb`. This release candidate ports only the still-needed behavior to the new tag; do not stack the historical commits.
