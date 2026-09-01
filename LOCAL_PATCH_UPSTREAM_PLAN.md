# Local Patch Upstream Plan — Execreations Hermes Agent

## Baseline and policy

- **Release candidate:** `v2026.8.31` / Hermes Agent `v0.21.0`
- **Maintained branch:** `execreations`
- **Policy:** reset to an upstream **release tag**, replay only still-needed carries, validate, then cut over. `origin/main` drift is telemetry—not a stable-update target.
- **Current state:** candidate validated; no upstream issue or PR identifiers are claimed here unless independently checked at submission time.

## Candidate upstream contributions

| Local area | Recommended scope | Must preserve |
|---|---|---|
| Title cleanup | Small focused PR. | Reject conversational/opening-response artifacts while retaining legitimate concise titles. |
| Hindsight scheduling | Separate provider/runtime PR. | Bounded joins, retain ordering, and no prefetch starvation of `sync_all`. |
| Current-turn recall + framing | Memory-safety PR; split only if call sites become independent. | Opt-in default, async path unchanged, recalled text treated as informational background. |
| Auto-recall telemetry | Separate state/Insights PR. | Additive schema migration, attempt/append evidence, no session DB reset. |
| Codex app-server memory parity + malformed sidecars | Focused transport-parity PR. | API-only injected context, no persisted synthetic user text, malformed optional metadata cannot abort a turn. |
| Embedded Hindsight environment and runtime probe | Small provider-runtime PR after current-upstream inspection. | No ambient LLM base-url leak; accept the `uvx`-isolated Embed launcher without false-negative host-stack imports. |
| Email aligned DKIM `temperror` retry | Security-focused PR. | Trusted Authentication-Results gating, aligned re-verification, DNS-timeout-only retry, mailbox-bound fail-closed transaction, acknowledgement before dispatch, `dkimpy` release-cutoff exemption, and regression tests. |

## Submission rules

1. Before preparing a PR, inspect current `origin/main` and active upstream PRs/issues for semantic overlap; titles alone are not evidence.
2. Test the proposed delta from a clean worktree against the then-current upstream base.
3. Keep the local carry until a released upstream version contains the same defaults, call sites, safety constraints, and tests.
4. Refresh this plan and `LOCAL_PATCH_INVENTORY.md` in the same change that retires or narrows a carry.
5. Do not create duplicate tracker noise: when an upstream PR is close but incomplete, document the exact remaining local delta and contribute only after the overlap gate is met.

## Historical email source note

The maintained composite source `8937b537a4` supersedes split sources `9f08f13132`, `f76048bf52`, and `c1259f38eb`. Future upstream work starts from the composite behavior; never stack the historical commits again.
