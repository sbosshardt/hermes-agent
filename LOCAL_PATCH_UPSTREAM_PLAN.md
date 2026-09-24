# Local Patch Upstream Plan — Execreations Hermes Agent

## DEPLOYMENT VERIFIED — v2026.9.21 / Hermes v0.21.4

- Verified at **2026-09-24T02:10:46.092180+00:00**; supervisor `hermes-v2026-9-21-cutover-final-20260924T015737.service`.
- Deployed runtime/source: `52f0372c61b51d28ac96526cec695a4cf452fd36`; tested code `ff00765fd37ee0ec078fa16048ebd66b53fd7ea4`.
- Scope: **seven gateways** (default, TowTargetDev, CardRewards, EthereumCase, pi-3b-admin, pi-4b-admin, Creative), dashboard and Nous proxy. Creative shares this runtime; the older separate-runtime exclusion below is historical.
- All nine new PIDs, per-profile required adapter records, default API v0.21.4, dashboard/API anonymous denial, authenticated proxy and external Hindsight health passed supervisor and independent finalizer checks. No fresh inbound human message round-trip is claimed.
- Fork candidate SHA pushed with exact expected-old lease and read back before this docs-only closeout. Runtime code is unchanged by this documentation commit.
- Copied-state rehearsal: 11/11 databases retained sessions/messages and integrity, with six telemetry columns; fresh verified post-stop rollback snapshot recorded in the cutover log.
- Aggregate remains **55,405 passed, 25 clean-tag-reproduced failures, 579 skipped**; accepted baseline risk, not a clean full-suite pass. Operator and documentation regression suite: 76 passed.
- Local cutover tooling and logs: `/home/hermes/.hermes/cache/release_validation/`. No upstream issue/PR/comment is authorized or created by this deployment.

## Historical pre-cutover checkpoint

The original audit below is preserved as historical evidence. Its candidate/not-deployed, pending cutover, and six-profile statements are superseded by the verified deployment record above. Patch rationales and upstream tracking remain applicable.

### Baseline and release policy

- **Release candidate:** `v2026.9.21` / Hermes Agent `v0.21.4`, in an isolated worktree; not deployed or pushed as of this record.
- **Maintained branch:** `execreations`, still `419ef2a84d` on the previous release. Its fork branch is synchronized at that SHA; new candidate ancestry does not fast-forward from it.
- **Policy:** use upstream *stable tags*, port only still-needed behavior, validate with rollback in hand, then cut over serially. Movement on `origin/main` alone is not a new stable release.
- **GitHub authorization:** Samuel approved a guarded `--force-with-lease` update of `sbosshardt/execreations` **only after** all candidate validation and rollback gates pass, using a freshly re-read exact remote SHA. That approval does not authorize a public issue, PR, comment, or unrelated branch push; none has occurred.

### Candidate upstream contributions (proposals, not submissions)

| Local area | Recommended scope | Preserve/test before any PR |
|---|---|---|
| Title quality | Small independent PR against current upstream main. | Reject response-shaped/greeting titles and retain concise typed-topic fallback. |
| Memory framing | Independent prompt-injection-hardening PR. | Recalled material remains informational, including alternate transports. |
| Prefetch scheduling and session fence | Split into independent executor-starvation and join/session-boundary PRs if call sites remain separable. | No `sync_all()` starvation; bounded joins; no late or stale session publication; no turn-start inline leak. |
| Auto-recall state/Insights telemetry (candidate integrated; aggregate baseline comparison complete, deployment gated) | State/Insights PR only after operational go/no-go, fresh migration and deployment verification; aggregate suite was **nonzero**, not a clean pass. | Additive schema; durable per-session attempt/success/failure/latency, read-only legacy Insights compatibility, best-effort bounded write. Chat observation proves unique freshly composed current turn in final assembled payload after middleware, **not** provider acceptance; Codex requires acknowledged exact fresh recall input. No durable append counter. |
| Scoped SQLite writer-lock audit coverage (candidate test-only `82066532f0`, `ff00765fd3`) | Consider a narrow tests-only upstream PR independently from runtime telemetry after deployment; compare current upstream audit before posting. | Only exact `SessionDB._execute_write` conditional and in-class `_write_lock` acquire/yield/finally-release qualify; negative changed-context/shadow/deferred-function/lambda tests and temporary-DB owner-thread execute/commit/rollback/timeout cases. Lexical AST tripwire, not arbitrary rebinding/alias proof. |
| Codex app-server context parity and recovery (candidate integrated; aggregate baseline comparison complete, deployment gated) | Focused transport PR (may split API-content parity from thread-recovery continuity) after operational go/no-go and deployment verification; aggregate suite was **nonzero**. | API-only injected memory/plugin context, original stored user text, lower-trust historical recovery (never developer instructions), sidecars only for acknowledged wire input with durable provenance, including clean overrides, late-first-persist, and real in-place compaction. A late first flush after a clean acknowledged retry must never infer raw content as wire from override/sanitize divergence; only the authenticated ACK stamp may supply or clear it (`14990275e3`). |
| Embedded Hindsight launcher | Small provider-runtime PR. | `hindsight-embed` lightweight `uvx` wrapper works without heavy host imports and without ambient base-URL credential leakage. |
| Email aligned DKIM `temperror` retry | Security-focused PR with an explicit trust-boundary review. | Trusted sender and aligned auth only; retry transient DNS errors, durable mailbox/profile-scoped state across reconnect/restart, ack before dispatch, `dkimpy` packaging, no untrusted dispatch. |

### Tracking and overlap gate

- No issue/PR identifier in this document has been claimed as a current exact tracker. Before opening or commenting, inspect current `origin/main`, release tag, linked PR chains, and exact diffs; search for an active issue/PR that already owns the behavior.
- If an active PR includes the same hunk, do not duplicate it. If it addresses only an adjacent path, state the missing call site/default/test and provide a tested comparison before contributing. Do not post vague “we also see this” comments.
- Prepare each PR on a clean upstream-main worktree (not the deployed branch); use regression tests and evidence-backed PR body. Preserve local carries until a *released* upstream tag has the same semantics, defaults, and call-site/test coverage.
- Keep this plan and `LOCAL_PATCH_INVENTORY.md` synchronized whenever a carry lands, splits, or retires. The old synchronous current-turn recall carry is currently retired by deployment choice, not by verified upstream equivalence; re-audit if the option is restored.
- Final aggregate evidence at code/test commit `ff00765fd3`: **55,405 passed, 25 failed, 579 skipped** across 4,793 files, plus one first-attempt TUI flake that passed retry. All 25 exact final failed IDs reproduced on clean `v2026.9.21`; the two previously candidate-only writer-lock audit failures are resolved. This is a baseline comparison, **not** a clean full-suite pass. A subsequent documentation-only ledger commit does not change the tested runtime/test files.

### Historical email source note

Prior composite email source `8937b537a4` superseded split sources `9f08f13132`, `f76048bf52`, `c1259f38eb`. This release candidate ports only the still-needed behavior to the new tag; do not stack the historical commits.
