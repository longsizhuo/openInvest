---
name: invest-backup
version: 0.1.0 # x-release-please-version
description: Back up / restore openInvest's local state — memory/ (holdings, strategy, user profile, committee records, dream logs) + db/ (trade ledger, job run history, market-data cache) + .env (SMTP/API credentials) + user_profile.json. All of this data is .gitignore'd with no historical versions in git, so a single accidental overwrite (e.g. slipping and running some one-off migration/init script) means real data loss — no git revert available. **Proactive trigger scenarios** — "backup invest data / 备份一下 openInvest 的数据", "my holdings/strategy look wiped / 我的持仓/策略好像被清空了", "invest data is lost / invest 数据丢了", "restore invest backup / 恢复一下 invest 的备份", before any reinstall/migration of the openInvest deployment on this machine, or right before running an unfamiliar migration/init script (back up first, then act).
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, backup, restore, ops, 备份, 恢复, 迁移]
---

# Invest Backup Skill

## Why this skill exists

openInvest's real data (holdings/strategy/user profile/committee records under `memory/`, trade ledger/job history under `db/`) is **entirely `.gitignore`d** — deliberately (privacy: it contains real assets/salary/trades), but the price is that git can't help at all. There was an incident on 2026-07-08: a one-off migration script named `migrate_profile.py` (with no safety guard whatsoever) was run directly once, overwriting `user.md` / `strategy.md` / `portfolio.md` with hard-coded demo defaults; `daily_report` then exited early every day because `target_assets` had become empty, emails stopped entirely from that point on, and the user had no idea until they noticed the missing emails and traced it back.

**In any of the following scenarios, proactively use this skill**:
- Before running any unfamiliar migration / init / bulk script that writes to `memory/` or `db/` → `backup` first
- The user says holdings, strategy, or committee records look wrong / wiped → `backup` first (even if the current state is already broken, save the "broken state" — otherwise you lose even the diagnostic material), then investigate
- Migrating to a new machine / reinstalling the deployment → `backup` to package, `restore` on the new machine

## Usage

```bash
plugin/skills/invest-backup/scripts/run.sh backup [output_dir]   # defaults to $INVEST_ROOT/.backups/
plugin/skills/invest-backup/scripts/run.sh restore <zip_path> [--force]
plugin/skills/invest-backup/scripts/run.sh list
```

- **backup**: packs `memory/`, `db/*.sqlite`, `db/*.db`, `.env`, `user_profile.json*` into a single UTC-timestamped zip. The data directory is resolved via `openinvest.paths.INVEST_ROOT` (same precedence order as the rest of the backend: `INVEST_HOME` env → repo-marker detection → cwd) instead of re-guessing paths in shell.
- **restore**: unpacks a zip back into the data directory. **By default it refuses to overwrite a `portfolio.md` that already contains real holdings/cash** (same criterion as the 2026-05-10 incident defense in `lifecycle_cmds.py:_write_v2_portfolio`: cash > 0 in any currency, or non-empty holdings, counts as real data) — to confirm the overwrite you must explicitly pass `--force`. With or without `--force`, the current state is automatically backed up before restoring, so the operation itself is reversible.
- **list**: lists existing backups, newest first, with sizes.

## What is not included

- The `memory/.backtest*` family of directories (historical backtest caches, tens of MB, regenerable with scripts like `scripts/backtest_committee.py` — not irreplaceable data) — this skill currently does **not exclude** them, because the size is still acceptable and `backup` semantically means "the entire data directory"; if `memory/` ever grows too large to package comfortably, exclude them separately then.
- `db/*.sqlite-journal`, `*.db-shm`, `*.db-wal`: SQLite runtime temp files — they are rebuilt automatically after a restore, and carrying them along could actually capture a half-committed state.
- `*.lock`: fcntl file locks, cleared automatically on process restart, no need to keep.

## Relationship to migrate_profile.py-class incidents

This skill only solves "can it be recovered once lost" — it does **not** solve "why did it get overwritten". If you see a script in this repo like `migrate_profile.py` that calls `store.write(...)` directly with no safety guard, follow the pattern of `_write_v2_portfolio` in `src/openinvest/skill_cmds/lifecycle_cmds.py` (check for existing real data before overwriting; on refusal require explicit `force=True`; auto-backup before overwriting) and retrofit the same guard — that is the real fix; this skill is only the last safety net.
