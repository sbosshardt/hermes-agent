# Local Patch Inventory

- Baseline: upstream tag `v0.23.0` plus the reviewed cutover tip `90214c887a284bfcd29d142175b1c70e11cbdf7e`
- Published branch: `execreations`
- Previous published tip: `8550e159c80caabfc54240f20464da55c2af6491` on `backup/pre-v0230-execreations-20261107`
- Running generation: the cutover workspace built from `90214c887a284bfcd29d142175b1c70e11cbdf7e`, not this checkout
- Last refreshed: 2026-11-07 17:35 America/Los_Angeles

## Active carries on the v0.23.0 line

These twelve commits are the reviewed local carries above `v0.23.0`. The exact running code is `90214c887a284bfcd29d142175b1c70e11cbdf7e`, also stored as `candidate/v0230-cutover-20261107`.

- `af7cd66ca8` fix(memory): retain informational and malformed-context boundaries
- `100ab68d01` fix(memory): retain independent lanes and bounded session fences
- `e23f7f5218` fix(titles): retain typed first-clause conversational fallback
- `d39aeb497b` fix(streaming): reset stale streak once at each fresh user turn
- `dc4476b3b1` Port bounded recall telemetry and assembled chat attribution
- `d2d988422e` Adapt writer lock audits to split telemetry ownership
- `15fab01e62` fix(codex): preserve lower-trust ACK-bound context and compaction provenance
- `d09b1b2662` fix(email): retain aligned transient DKIM retry with durable mailbox fencing
- `9a5a7d6868` feat(vault): preserve scoped opt-in unattended Bitwarden with native origin guards
- `ef005b3339` build(email): declare resolved host-scoped DKIM runtime dependencies
- `e7158f7654` fix(session): forward worker display_metadata sidecar writes
- `90214c887a` fix(session): persist worker auto-recall metrics and schema history

The Hindsight prefetch lock fix is not in this repo. It is external commit `8b913ee03f7d90bcdf2510f3ddb1b49c886e3dbf`.

## Retired from the published tip

The pre-cutover `execreations` history remains recoverable at `backup/pre-v0230-execreations-20261107`. It is no longer the published tip because the live gateway now runs the v0.23.0 cutover generation.
