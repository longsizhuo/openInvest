# paper_fleet — 每日前瞻纸面委员会(方案二,ADR-022 更新节)

**目的**:持续生产**不受模型升级影响**的干净样本。决策时未来尚不存在 ⇒ 任何未来
模型都无记忆可穿越(2026-07 deepseek-v4-flash cutoff 事件实证:历史回填桶是相对
模型的,前瞻样本是唯一免疫源)。

⚠️ **评分口径**:`jobs/verdict_review` 每天 02:00 只把 live 写进 `verdict_review.jsonl`;舰队样本(本目录前瞻部分)每天另打一遍 30d 标签,**只进**裁决旁的同类决议查表 `.dreams/confidence_lookup.json`(每格记 n_live / n_fleet),不进命中率页和纪律台账。舰队进 jsonl 的全量重建仍是手动`uv run python -m openinvest.jobs.verdict_review --include-backtest`(纯本地计算,零 API 费用)。

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

crontab(北京 06:30,美盘收盘后;已于 2026-07-24 挂上,07-25 修 PATH——
cron 的 /bin/sh 不含 ~/.local/bin,首夜因 `uv: not found` 空跑一次):

```
30 22 * * * export PATH="$HOME/.local/bin:$PATH"; cd /home/ubuntu/projects-review/invest && BACKTEST_WORKERS=25 uv run python -m scripts.backtest_committee --prospective --assets "$(uv run python -c "import yaml; print(','.join(yaml.safe_load(open('experiments/paper_fleet/universe.yml'))['symbols']))")" >> memory/.backtest/fleet_daily.log 2>&1
```

## 试跑臂：T2 CONFIDENCE 定义(D11 P2,2026-10)

`--prospective --t2-confidence-arm`:对照臂(上面那份,写 `memory/.backtest/`)跑完后,
同一批新鲜标的再跑一整轮委员会,只有 CIO prompt 不同——删掉"三方一致 confidence ≥ 0.85"
数字规则,加上 CONFIDENCE 定义("30 个日历天后被 verdict_review 判为命中的概率";HOLD
命中 = 30 天涨跌留在该资产正常波动带内)。T2 臂写 `memory/.backtest_t2conf/<今天>/`,
verdict_review / dreaming / #141 样本计数都只读 `.backtest`,碰不到它。成本 ×2(≈ ¥3/月 → ¥6/月)。

- 变体在 `scripts/backtest_committee.py:build_cio_prompt_t2`;live 委员会不 import 本脚本,
  live CIO prompt 逐字节不变(`tests/test_fleet_confidence_t2.py`)
- cio.md 改了那两处锚点措辞 → T2 臂在花 LLM 钱前直接报错退出(对照臂照常落盘),要同步改变体
- 开 / 停:在上面 crontab 那行 `--prospective` 后加 / 删 `--t2-confidence-arm`
- 判读口径(臂、指标、护栏、停止规则)按私有归档里的预注册,不在本 repo

## 标的池

- `universe.yml` — 舰队每日 50 标的(八资产类别)
- `universe_l2/l3/l4.yml` — 历史回填扩层清单(L2 +100 / L3 +240 / L4 +389,
  与舰队共用 MarketStore 缓存;L4 回填于 2026-07-24 按预算暂停,断点续跑随时可续)

## 保护闸(2026-07-24 CR 后加)

- **行情新鲜度**:逐标的检查缓存最新 bar,**不是当日 bar 就跳过**并打印修复命令——决不拿
  陈旧收盘价出当天 verdict(那会让 verdict_review 拿真实后市给错标样本打分)。逐标的判定
  顺带覆盖周末、各市场假日不同步与加密 7×24。
  ⚠️ **2026-07-25 ~ 2026-09-27 的周末目录是污染样本**:当时阈值是"≤5 天",周末用周五
  收盘价各出了两份重复 verdict(22 天 × 50 ≈ 1100 份)。评分/统计时按决策日
  `weekday()>=5` 且非加密标的剔除;2026-10-07 起已改为严格当日。
- **成本闸**:`--limit N` 在 prospective 下有效(只跑前 N 个标的);`--step` 等回填
  参数一律拒绝而非静默忽略。
- **失败可见**:结尾打印 `ok/total`,有失败则 exit 1,cron 日志不会静默成功。
- **币种**:按 yfinance 后缀映射(.HK→HKD/.T→JPY/.SS→CNY/裸 ticker→USD),
  取代原先"非 NDQ.AX 一律 CNY"的硬编码。

## 产出口径

50 条/交易日 ≈ 每年 ~12,600 条永久干净样本,成本 ≈ ¥3/月。累计样本随
`jobs/verdict_review` 评分后进入 `memory/.dreams/verdict_review.jsonl`,与回填
语料同一账本、同一分桶纪律(cutoff 单一可信源 `review_calc.CONTAMINATION_CUTOFF`)。
