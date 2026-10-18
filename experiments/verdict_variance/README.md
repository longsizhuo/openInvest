# verdict_variance — 同输入多次抽样，裁决到底有多抖（评审路线 Sprint 2 · A4）

**单次研究，不进生产路径**：生产代码从不 import 这里。结论带样本量，别当成稳定的系统属性。

问题：委员会的最终裁决是一次 LLM 采样。同一份输入再跑一次，方向、自报置信度、建议金额会变多少？
（A5 的校准表量的是"自报分和命中对不对得上"；这里量的是"同一输入下这个数自己稳不稳"。）

## 怎么跑

```bash
# key 只从 env 读、不打印
export $(grep -E '^(DEEPSEEK_API_KEY|LLM_API_KEY|LLM_BASE_URL|LLM_MODEL)=' <prod>/.env | xargs)
# INVEST_HOME 必须是数据拷贝：memory/*.md + memory/.committee + db/market_data.db（没有 events.db / insights.db / trades.db）
touch <拷贝>/.variance_copy_ok          # 哨兵文件；或者每次传 --i-know-this-is-a-copy
INVEST_HOME=<拷贝> uv run python experiments/verdict_variance/run_variance.py --dry-run          # 只列计划 + 统计自检
INVEST_HOME=<拷贝> uv run python experiments/verdict_variance/run_variance.py --n 3 --limit 30   # ≈90 次委员会
```

对 `memory/.committee/<date>/<sym>.md` 里最近 `--limit` 条 live 裁决各重跑 `--n` 次 Direct 委员会
（`run_committee_session(symbols=[sym])`，`--max-rounds` 默认 1 = Direct CLI 默认），按标的并发、同标的串行；
增量写明细文件，重启同一条命令会跳过已完成的重跑。

脚本会写 `INVEST_HOME` 下的 `memory/.committee`、`.dreams`、`db/`，所以拒绝：没设 `INVEST_HOME`、目标是 git checkout、
没有哨兵文件也没传 `--i-know-this-is-a-copy`（PyPI 形态的生产数据目录没有 `.git`，光靠 checkout 检查拦不住）。

## 输出：明细不进仓库，只提交匿名汇总

- **逐条明细**（每条裁决的日期、标的、每次重跑的 verdict / confidence / alloc）= 真实账户的逐日裁决和金额，
  默认写到 `INVEST_HOME/memory/.dreams/verdict_variance/variance_<日期>.json`（gitignored 的数据目录），
  脚本拒绝把它写进仓库。
- **匿名汇总** `summary_<日期>.json`（同目录）可以提交：标的换成 A/B/C/D（按重跑数降序）、无日期、无金额、无逐条明细。
  本目录 [`data/summary_2026-10.json`](data/summary_2026-10.json) 就是这样来的。

## 结果（一次运行：30 条裁决 × 3 次 = 90 次，0 次失败，模型 deepseek-v4-flash）

| 指标 | 数值 |
|---|---|
| 方向翻转率（同一条裁决的 3 次重跑方向类不全相同） | **26.7%**（8/30，Wilson 95% 区间 14–44%） |
| 两两翻转率（3 对里方向不同的平均占比） | 17.8% |
| 自报置信度标准差（每条 3 次，取均值） | **0.000**（90 次全是 0.62） |
| 建议金额变异系数（有任一次非 0 的 9 条；另 21 条三次全 0 不算） | 均值 1.40，中位 1.73 |
| 重跑方向与原决议不同的比例 | 13.3%（12/90，混了"输入换成运行当天"，仅作参照） |

按标的（**样本量以这里为准**：30 条裁决只对应 4 个不同标的，同标的的 5–10 条裁决共享同一份运行当天的输入）：

| 标的（匿名） | 条数 | 重跑数 | 裁决分布 | 两两翻转率 |
|---|---|---|---|---|
| A | 10 | 30 | ACCUMULATE 1 / HOLD 29 | 7% |
| B | 8 | 24 | HOLD 24 | 0% |
| C | 7 | 21 | ACCUMULATE 11 / HOLD 10 | **52%** |
| D | 5 | 15 | ACCUMULATE 1 / HOLD 14 | 13% |

**结论（n=4 个输入 × 每个 15–30 次重跑，单次运行、单模型）**：

1. **方向近似"大多数 HOLD + 偶发翻转"，但个别标的是抛硬币**：标的 C 在同一份输入上 21 次里 11 次 ACCUMULATE、10 次 HOLD，
   生产上看到的那一次裁决就是其中一次抽签；其余三个标的 93–100% 稳定 HOLD。8 条翻转的裁决里 6 条是 C。
2. **自报置信度没有方差，因此没有信息量**：90 次一次不差全是 0.62，包括 13 次 ACCUMULATE。这与 A5 回算一致——
   2026-09-16 起生产里 84 条裁决有 75 条（89%）自报恰好 0.62（见 `docs/wiki/12-verification.md` 8.2）。
   置信度没法区分"稳 HOLD"和"抛硬币的 ACCUMULATE"。
3. **建议金额随抽签大幅摆动**：C 的 11 次 ACCUMULATE 之间，建议金额最大值约为最小值的 2.7 倍。
   CV 中位 1.73 ≈ √3 正是"三次里只有一次非 0"的形态，即金额的方差主要来自方向翻转，而不是同方向内的微调。

## 局限（读结论前先看）

- **重跑用的是运行当天的输入，不是原决议日的输入**。所以测的是同输入抽样噪声，不是"原裁决能否复现"。运行日是周末，
  行情静止（最后一根是上一个交易日），同一标的的各次重跑看到的行情一致；宏观观点由每次重跑重新生成（Direct 路径本来如此），
  所以抽样噪声里含宏观角色那一次采样。
- **拷贝是部分上下文**：没有 events.db / insights.db，transcript 的 Input Health 记 `event_brief` / `valuation_brief` / `prior_insights`
  为空。生产里这些非空，上下文更长时方差可能不同——这里量的是缺这三块时的方差。
- **有效样本是 4 个输入**，不是 30 条；每个输入内是 15–30 次近似独立重跑。方向翻转率的 95% 区间按 30 条算，偏乐观。
- 单次运行、单模型、`--max-rounds 1`；模型/提示词变化后需重测。
- 未做任何"多次抽样取多数"的方案对比——这里只测量。
- 逐条明细不在仓库里，本目录的汇总无法复算到条；要复现请自己跑一遍（结果会因抽样而不同）。
