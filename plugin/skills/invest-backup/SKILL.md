---
name: invest-backup
version: 0.1.0
description: 备份 / 恢复 openInvest 的本地状态——memory/（持仓、策略、用户画像、委员会记录、dream 日志）+ db/（交易台账、job 运行历史、行情缓存）+ .env（SMTP/API 凭据）+ user_profile.json。这些数据全部 .gitignore，git 里没有任何历史版本，一旦被误覆盖（例如手滑直接跑了某个一次性迁移/初始化脚本）就是真实数据丢失，没有 git revert 可用。**主动触发场景**："备份一下 openInvest 的数据"、"backup invest data"、"我的持仓/策略好像被清空了"、"invest 数据丢了"、"恢复一下 invest 的备份"、"restore invest backup"、任何要在这台机器上重装/迁移 openInvest 部署之前、或者刚要跑一个陌生的迁移/初始化脚本之前（先备份再动手）。
platforms: [linux, macos]
metadata:
  hermes:
    tags: [investing, backup, restore, ops, 备份, 恢复, 迁移]
---

# Invest Backup Skill

## 为什么需要这个 skill

openInvest 的真实数据（`memory/` 下的持仓/策略/用户画像/委员会记录，`db/` 下的交易台账/job 历史）**全部 `.gitignore`**——这是故意的（隐私：含真实资产/工资/交易），但代价是 git 完全帮不上忙。2026-07-08 就出过一次事故：一个叫 `migrate_profile.py` 的一次性迁移脚本（没有任何 safety guard）被直接跑了一次，把 `user.md` / `strategy.md` / `portfolio.md` 覆盖成写死的 demo 默认值，`daily_report` 因为 `target_assets` 变空而每天早退、邮件从此全断，用户完全没有感知直到发现收不到邮件才追出来。

**遇到下面任一场景，主动用这个 skill**：
- 要跑任何陌生的迁移 / 初始化 / 批量写 `memory/` 或 `db/` 的脚本之前 → 先 `backup`
- 用户说持仓、策略、委员会记录看起来不对/被清空了 → 先 `backup`（哪怕现在的状态已经是坏的，也要把"坏状态"存一份，不然连诊断素材都没了），再排查
- 要迁移到新机器 / 重装部署 → `backup` 打包，新机器上 `restore`

## 用法

```bash
plugin/skills/invest-backup/scripts/run.sh backup [output_dir]   # 默认存到 $INVEST_ROOT/.backups/
plugin/skills/invest-backup/scripts/run.sh restore <zip_path> [--force]
plugin/skills/invest-backup/scripts/run.sh list
```

- **backup**：把 `memory/`、`db/*.sqlite`、`db/*.db`、`.env`、`user_profile.json*` 打成一个带 UTC 时间戳的 zip。数据目录走 `openinvest.paths.INVEST_ROOT` 解析（和后端其余代码同一套优先级：`INVEST_HOME` env → 仓库标记探测 → cwd），不在 shell 里重复猜路径。
- **restore**：从 zip 解回数据目录。**默认拒绝覆盖已含真实持仓/现金的 `portfolio.md`**（判定口径和 `lifecycle_cmds.py:_write_v2_portfolio` 的 2026-05-10 事故防御同一套：cash 任一币种 > 0 或 holdings 非空就算真实数据）——确认要覆盖必须显式加 `--force`。不管有没有 `--force`，恢复前都会先把当前状态自动备份一份，操作本身可逆。
- **list**：列出已有备份，按新到旧排序，附大小。

## 不包含什么

- `memory/.backtest*` 系列目录（历史回测缓存，几十 MB，可以用 `scripts/backtest_committee.py` 之类的脚本重新生成，不是不可再生数据）——本 skill 目前**没有排除**它们，因为体积尚可接受且 `backup` 语义上是"整个数据目录"；如果 `memory/` 体积涨到不方便打包，再单独排除。
- `db/*.sqlite-journal`、`*.db-shm`、`*.db-wal`：SQLite 运行时临时文件，恢复后会自动重建，带上反而可能是半提交状态。
- `*.lock`：fcntl 文件锁，进程重启后自动清空，不需要保留。

## 和 migrate_profile.py 类事故的关系

这个 skill 只解决"丢了能不能找回来"，**不解决"为什么会被覆盖"**。如果你在这个仓库里看到类似 `migrate_profile.py` 这种直接 `store.write(...)` 且没有 safety guard 的脚本，参考 `src/openinvest/skill_cmds/lifecycle_cmds.py` 里 `_write_v2_portfolio` 的模式（覆盖前检查是否已有真实数据，拒绝则要求显式 `force=True`，覆盖前自动备份）给它补一个同款守卫——这才是治本，本 skill 只是最后一道安全网。
