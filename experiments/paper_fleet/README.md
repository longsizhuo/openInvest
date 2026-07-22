# paper_fleet — 前瞻纸面委员会舰队

**目的**：持续生产**不受模型升级影响**的干净训练/评估样本（ADR-022 更新节）。
每天对 50 个 symbol 跑 Direct 委员会（纸面，独立 INVEST_HOME），`verdict_review`
在 30/90 天后自动用真实后市回填评分。50/天 → **73 天攒满 3650 条**。

为什么它是唯一的"永久干净"源：决策时未来尚不存在——横截面回填桶是相对模型的
（升级到含新语料的模型整桶变脏），前瞻运行对任何未来模型都免疫。

## 文件

| 文件 | 作用 |
|---|---|
| `universe.yml` | 50 标的池（美股/ETF/债/商品/加密/港股/A股ETF/澳；与真实持仓无关） |
| `run_fleet.py` | `--bootstrap` 初始化舰队 home；默认跑全池一轮，日志 jsonl |

## 启用步骤（⚠️ merge 进 main 后再挂 cron）

```bash
# 1. 初始化（幂等）
INVEST_HOME=~/openInvest-fleet uv run python experiments/paper_fleet/run_fleet.py --bootstrap
# 2. 放密钥（不自动拷贝）：~/openInvest-fleet/.env 里配 DEEPSEEK_API_KEY
# 3. 试跑 3 个确认链路通
INVEST_HOME=~/openInvest-fleet uv run python experiments/paper_fleet/run_fleet.py --limit 3
# 4. crontab -e 加两行（北京时间 06:30 跑舰队——美盘收盘后；07:30 回填复盘）
30 6 * * * cd <repo> && INVEST_HOME=$HOME/openInvest-fleet uv run python experiments/paper_fleet/run_fleet.py >> $HOME/openInvest-fleet/cron.log 2>&1
30 7 * * * cd <repo> && INVEST_HOME=$HOME/openInvest-fleet uv run python -m openinvest.scheduler.runner --once verdict_review >> $HOME/openInvest-fleet/cron.log 2>&1
```

（crontab 里的重定向目标 `~/openInvest-fleet/` 由 `--bootstrap` 建好，无 fresh-install
静默失败问题——PR #240 CR 阻塞项的教训。）

## 成本口径（粗估，DeepSeek 按 token 计费）

单次 Direct `run_committee` ≈ 4 角色 + CIO 若干万 token，约 ¥0.05–0.15/次；
50 symbol/天 ≈ **¥3–8/天**（试跑后用 `fleet_runs.jsonl` 的实际耗时/账单校准）。
成本闸：`--limit N` 随时降池。

## 已知边界

- **中性组合 caveat**（ADR-022 §6 同款）：舰队 home 是 cash-only，verdict 分布缺
  集中度维度，不可外推 live 用户
- 择时 alpha 只能锚定同资产 buy-and-hold（ADR-022 §7 幸存者约束），universe 是
  "今天还活着"的资产池，禁止外推"选股能力"
- Stage 0 同日缓存：同一天重复跑同 symbol 会直接读已有 transcript，不重复计费
