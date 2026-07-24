# paper_fleet — 每日前瞻纸面委员会(方案二,ADR-022 更新节)

**目的**:持续生产**不受模型升级影响**的干净样本。决策时未来尚不存在 ⇒ 任何未来
模型都无记忆可穿越(2026-07 deepseek-v4-flash cutoff 事件实证:历史回填桶是相对
模型的,前瞻样本是唯一免疫源)。`verdict_review`(生产 scheduler 既有 job)在
30/90 天后自动用真实后市回填评分。

## 形态(2026-07-24 重设计,v1 的独立 INVEST_HOME 方案已退役)

- **就在原仓库跑,用原有 .env**,零新增配置、零新组件
- 决策写 `memory/.backtest/<今天>/`——与真实 `.committee/` 账本天然隔离,
  gitignore 已覆盖,夜间 restic 备份自动带上
- 上下文完整:事件账本、dreaming insights 全在(独立空目录的"失忆委员会"问题不存在)
- 组合画像走 backtest 既有的中性硬编码(ADR-022 §6):真实持仓不会混进纸面决策,
  代价是 verdict 分布缺集中度维度,不可外推 live
- 入口:`scripts/backtest_committee.py --prospective`(只跑今天,周末自动跳过,
  与回填参数互斥;Contaminated 章恒为 false)

## 运行

```bash
# 手动跑一天(50 标的 ≈ 2 分钟 @ BACKTEST_WORKERS=25,按 2026-07 实测价 ≈ ¥0.1)
SYMS=$(uv run python -c "import yaml; print(','.join(yaml.safe_load(open('experiments/paper_fleet/universe.yml'))['symbols']))")
BACKTEST_WORKERS=25 uv run python -m scripts.backtest_committee --prospective --assets "$SYMS"
```

crontab(北京 06:30,美盘收盘后;已于 2026-07-24 挂上):

```
30 22 * * * cd /home/ubuntu/projects-review/invest && BACKTEST_WORKERS=25 uv run python -m scripts.backtest_committee --prospective --assets "$(uv run python -c "import yaml; print(','.join(yaml.safe_load(open('experiments/paper_fleet/universe.yml'))['symbols']))")" >> memory/.backtest/fleet_daily.log 2>&1
```

## 标的池

- `universe.yml` — 舰队每日 50 标的(八资产类别)
- `universe_l2/l3/l4.yml` — 历史回填扩层清单(L2 +100 / L3 +240 / L4 +389,
  与舰队共用 MarketStore 缓存;L4 回填于 2026-07-24 按预算暂停,断点续跑随时可续)

## 产出口径

50 条/交易日 ≈ 每年 ~12,600 条永久干净样本,成本 ≈ ¥3/月。累计样本随
`jobs/verdict_review` 评分后进入 `memory/.dreams/verdict_review.jsonl`,与回填
语料同一账本、同一分桶纪律(cutoff 单一可信源 `review_calc.CONTAMINATION_CUTOFF`)。
