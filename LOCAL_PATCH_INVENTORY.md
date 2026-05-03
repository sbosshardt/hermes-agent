# Local Patch Inventory (local/vps vs upstream `origin/main`)

Purpose: track every code change carried locally on `local/vps` that is not on upstream `origin/main`, and why it exists.

Last refreshed: 2026-05-03

## Scope / baseline

- Repo: `/home/hermes/.hermes/hermes-agent`
- Local branch: `local/vps`
- Upstream baseline: `origin/main`
- Generated from:
  - `git log --oneline origin/main..local/vps`
  - `git diff --name-only origin/main...local/vps`

---

## Current local-only commits (ahead of `origin/main`)

### 1) `3feb62853` — `local: use hindsight_embed runtime import check`

- Files:
  - `plugins/memory/hindsight/__init__.py`
- Why this exists:
  - Local safeguard around Hindsight embed/runtime import behavior to avoid regression in this environment.
- Keep/remove guidance:
  - Keep while local environment still needs the guard.
  - During rebase/update, if upstream already includes equivalent behavior, drop this patch (`git rebase --skip` when appropriate) and verify by checking for `hindsight_embed` logic in the same file.

### 2) `4120f2156` — `chore: sync ui-tui lockfile`

- Files:
  - `ui-tui/package-lock.json`
- Why this exists:
  - Adds missing optional `@emnapi` lockfile entries so `npm ci` succeeds for `ui-tui` without broad lockfile churn.
- Keep/remove guidance:
  - Keep as a narrow lockfile repair until upstream lockfile catches up.
  - Re-check on rebase; if `npm ci` passes without this delta, retire it.

### 3) `7bb86ffb9` — `feat(telegram): auto-rename forum topics on session title change`

- Source:
  - Cherry-pick of upstream PR #9921 commit `750beb9a2`.
- Files:
  - `agent/title_generator.py`
  - `gateway/platforms/telegram.py`
  - `gateway/run.py`
  - `tests/agent/test_title_generator.py`
  - `tests/gateway/test_auto_rename_topics.py`
  - `website/docs/user-guide/messaging/telegram.md`
- Why this exists:
  - Enables Telegram topic-title sync via `editForumTopic` when `platforms.telegram.extra.auto_rename_topics: true`.
  - Covers both auto-generated titles and manual `/title` changes.
- Keep/remove guidance:
  - Keep until upstream merges equivalent implementation and local branch rebases onto it.
  - After upstream adoption, drop local cherry-pick during rebase if duplicate.

---

## Note on "title-generator failure/runtime plumbing"

The `failure_callback` + `main_runtime` arguments in `agent/title_generator.py` were **already present in upstream `origin/main`** before applying PR #9921 locally. They are not a user-specific custom patch in this inventory.

During cherry-pick conflict resolution, we preserved:

- existing upstream failure signaling (`failure_callback`, `main_runtime`), and
- new PR #9921 callback (`on_title_set`) used to trigger Telegram topic rename.

---

## Runtime config change (host-local, not repo code)

Set on this host:

- `~/.hermes/config.yaml`
  - `platforms.telegram.extra.auto_rename_topics: true`

This is an environment config change, not tracked in this git repo.

---

## Maintenance workflow (quick)

After any future local patching, refresh this file with:

1. `git fetch origin`
2. `git log --oneline origin/main..local/vps`
3. `git diff --name-only origin/main...local/vps`
4. Update this inventory with commit IDs, files, reason, and retirement condition.
