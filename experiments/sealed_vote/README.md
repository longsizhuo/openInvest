# sealed_vote — B4 密封投票 A/B 臂（评审路线 B4）

**只加实验臂，不改任何 live 裁决**（治理红线：先审计后实验）。live 的 `.committee/`、邮件、日报、对照臂 `.backtest/`、
T2 臂 `.backtest_t2conf/` 一个字节都不变；本实验的所有产物在数据目录（`memory/.backtest_sealed/`、
`memory/.dreams/verdict_review_sealed.jsonl`，gitignored），仓库里只有代码、文档和匿名汇总。

> 一句话翻译：舰队每天多跑一臂对照（角色不互相看 + 每个标的跑三次投票），你收到的裁决不变；35 天后出判读。

## 问题与依据

- A4（[`../verdict_variance/`](../verdict_variance/README.md)）：同一份输入重跑 3 次，30 条裁决里 26.7% 方向翻转，
  有个标的 21 次里 11 次 ACCUMULATE / 10 次 HOLD；自报置信度 90/90 恒为 0.62，没有信息。**单次抽样的方差和信号同量级。**
- 评审调研：多轮辩论导致趋同和囤现金；唯一有显著增益的干预是"保留分歧"。
- 于是 B4 = 角色先各自给出信号、互不可见（密封）→ 同一份输入独立抽样 N 次 → 按票汇总 → 用**投票分裂度**代替自报置信度。

本实验回答：**多次抽样取多数 + 分裂度**，比同一输入下的**单次抽样**更稳、更准吗？

## 设计

| | 对照臂（现有舰队产物） | 实验臂 |
|---|---|---|
| 目录 | `memory/.backtest/<日期>/<标的>.md` | `memory/.backtest_sealed/<日期>/<标的>.md` |
| 每标的抽样 | 1 次 `run_committee`（rounds=`INVEST_MAX_DEBATE_ROUNDS`，缺省 1） | N=3 次 `run_committee(max_debate_rounds=1)` |
| 输入 | 同日同标的：`market_data` / `regime_brief` / 中性持仓 / `macro_view` **逐字相同**（实验臂借对照臂这次跑出来的 `macro_view`） | 同左 |
| 裁决 | 那一次抽样 | 按票聚合（见下） |
| 置信度 | CIO 自报 | 最大票占比 |

### "密封"在代码里是什么（读 `core/committee/debate.py` 核实）

`max_debate_rounds=1` 时 `for round_idx in range(2, max(2, 1 + 1))` 是空循环：**没有 cross-challenge**。
Round 1 的 Quant / Risk 并行起跑，输入互不相交（Quant 只看行情/估值/情绪，Risk 只看持仓/长期模式）；CIO 只看到两份 Round-1 简报。
"完整辩论历史"和"辩论元信息"两块只在 `len(quant_history) > 1` 时才附加（本分支不会附加）。
一个小毛边（**未改**，改了就动 live）：`CommitteeReport.to_cio_brief()` 无条件打印 `=== ROUND 2 (cross-challenge 后的调整) ===`
标题，rounds=1 时它下面是两段空的"调整"，CIO 看到的是空标题。

### 聚合规则（`scripts/backtest_committee.py:aggregate_votes`，纯函数）

- **verdict**：严格多数（票数 > N/2，N=3 即 ≥2 票相同）；没有多数（N=3 三者各异、N=4 的 2:2 …）→ `HOLD` 并标 `split`。
- **confidence** := 最大票占比（N=3：1.0 / 0.67；三者各异 0.33）。
- **alloc** := 多数票那几次建议金额的**中位数**；HOLD（含 split）恒 0。
- **flip**：N 次的方向类（`EXPECTED_DIRECTION`，UNCLEAR=none）不全相同——与 A4 的翻转口径同一把尺子，数字可直接比。
- 落盘的 transcript 正文取**代表票**（多数票里最早的一次；split 取第 1 次，并在 CIO 备忘录末尾注明"票数分裂 → HOLD"）；
  `rewritten` / `forced_hold` 也是代表票的（split 的 HOLD 是投票结果，不是规则改写）。
- Input Health 块多四个字段：`arm="sealed_n3"`、`votes=[{verdict,confidence,alloc}×N]`、`flip`、`split`；其余字段与对照臂同口径。
- 任一次抽样抛异常 → 该标的整体失败、**不落盘**（下次续跑重来），保证臂内每一行都恰有 N 票；同日已落盘的标的跳过（断点续跑同口径）。

## 怎么开 / 停 / 跑

```bash
# 手动跑一天（对照臂 + 实验臂；T2 臂可同开）
BACKTEST_WORKERS=25 uv run python -m scripts.backtest_committee --prospective --sealed-arm --assets "$SYMS"
#   --sealed-n 3     每标的抽样次数（默认 3，最少 3）。开跑后别改：同目录混不同 N 的行不可比
#   --limit N        成本闸，只跑前 N 个标的
```

调度和 T2 臂一样：舰队的 crontab 行（不在仓库里，见 [`../paper_fleet/README.md`](../paper_fleet/README.md)）在 `--prospective` 后加
`--sealed-arm` 即开，删掉即停。**合并后等生产 checkout 自动 pull 了再改 crontab**（旧代码不认 flag，argparse 报错会让整条舰队 cron 失败）。

复盘不用另配 cron：`verdict_review` 每日 job（02:00 Asia/Shanghai）尾巴调 `run_arm()`，写

- `memory/.dreams/verdict_review_sealed.jsonl`：实验臂的每一行复盘（`source="arm:sealed"`）；
- `docs/verdict_accuracy_sealed.md`：稳定度 + 与对照臂的并排/配对对照（只含聚合，无 symbol，无金额）。

两者都**不进** live 命中率页、`verdict_review.jsonl`、`confidence_lookup.json`、公开 `accuracy_summary.json`
（`tests/test_arm_review.py` 逐项守：有无实验臂目录，live 产物逐项相同）。手动：
`INVEST_HOME=<数据目录> uv run python -c "from openinvest.jobs.verdict_review import run_arm; print(run_arm())"`。

## 成本（按 2026-10-15/16 舰队实测的 `llm_usage.jsonl` 单价口径，deepseek-v4-flash，含 KVCache 命中价）

一次 rounds=1 委员会 = Quant + Risk + CIO 共 3 次调用，均值约 ¥0.0041（Quant 0.00083 + Risk 0.00039 + CIO 0.0029）。

| | 每个交易日（≈46 个新鲜标的） | 每个周末日（3 个加密） |
|---|---|---|
| 现有舰队（对照臂 + T2 臂） | ≈278 次调用，¥0.40 | ≈18 次，¥0.03 |
| **新增：实验臂 N=3**（macro 借对照臂，不重跑） | **+414 次调用，≈ ¥0.57** | **+27 次，≈ ¥0.04** |

约 **+¥13 / 月**（22 个交易日 × 0.57 + 8–9 个周末日 × 0.04），总舰队 ≈ ¥9 → ¥22 / 月。标的间并发度不变（`BACKTEST_WORKERS`），
同一标的的 N 次抽样串行，所以实验臂墙钟 ≈ 对照臂的 3 倍、同时在飞的请求数不增加（不会更容易撞限流）；本机首跑 5 个标的并发，一个交易日的实验臂约 24 秒。
首跑（下节）实花 ¥0.62（538 次调用：43 次对照 + 129 次抽样，单次委员会 ≈ ¥0.0035，低于上面按舰队 KVCache 命中率测的 ¥0.0041）。

## 判据（开跑前冻结；看到数据后不改 N、聚合规则、判据）

**判读日期 = 开跑日 + 35 天**（开跑日 = `memory/.backtest_sealed/` 里最早的日期目录）。判读由 AI 起草、用户签字；
本表是"要不要提 live 切换提案"的门槛，**不是**切换本身——切换是另一个 PR，要附"一句话信号翻译 + 确定性回算差异报告"，要用户签字。

两条路任一条通过才提案（同一份报告里读，口径见 `docs/verdict_accuracy_sealed.md`）：

- **H1（准）**：实验臂整侧 30d「提升」的 95% CI 下界 > 0。提升 = 命中率 − 同尺子基率；CI = 命中率 Wilson 区间减基率（基率当常数，
  与 live 命中率页同口径）；30d 成熟可评分 n ≥ 30 才判。
- **H2（稳）**：配对（同日同标的）的**逐日方向翻转**（同一标的相邻决议日方向类不同）实验臂显著少于对照臂，**且** HOLD 占比不更高。
  "显著" = 只看两臂结果不同的那些对，精确符号检验双侧 p < 0.05；"HOLD 占比不更高" = HOLD 配对里"仅实验臂为 HOLD"
  没有显著多于"仅对照臂为 HOLD"。**功效闸**：判读日实验臂里"非全票一致"的行 < 30 → 判"功效不足"，既不算通过也不算否定。
- 都没过 → 关臂（crontab 删 flag），把 negative result 写进 `docs/wiki/12-verification.md`，不论好坏都公布。

怎么读 H2：对照臂每天只有 1 票，没有臂内重复可比，所以"稳定度"只能用逐日翻转这个量在两臂间同口径比较——输入的真实变化两臂共享
（同日同标的），差出来的部分是抽样噪声。臂内的"全票一致率 / 方向翻转率"（A4 口径，A4 基线 26.7%）是描述性的：它量的是**单票**的不稳，
不是聚合后的不稳，不能直接当 H2。分裂度是否带信息，看报告里"一致度 vs 30d 命中"那张表（n<30 不出数字）。
H1 / H2 是"二选一通过"，名义误报率约翻倍，所以这只是进入提案的门槛，不是证据终点。

## 已知局限（读结果前先看）

1. **这个 A/B 隔离的是"N=3 多数票 + 分裂度"，不是"辩论 vs 不辩论"。** 舰队对照臂本来就是 rounds=1
   （crontab 没设 `INVEST_MAX_DEBATE_ROUNDS`，缺省 1），已经是"密封"的单次抽样。live 现状并不一样：日报 `daily_report` 用
   `max_debate_rounds=4`，Web 默认 4，事件触发重跑读 `event.max_rounds`（默认 2），Direct CLI 默认 1。
   所以即使本实验通过，真要把 live 切到"密封 + 投票"还包含"4 轮 → 1 轮"这一步，那一步本实验**没有测**，提案里要单独回答。
2. **功效可能很低。** 舰队用中性持仓、没有事件/估值/情绪输入，裁决几乎全是 HOLD：2026-07-24 起舰队 3,462 行里 HOLD 占 94%（含周末重复样本；
   10 月 97%），0 条 BUY / SELL / TRIM。HOLD 的"提升"≈ 0，所以整侧提升 CI 下界 > 0（H1）很难；票数一致的 HOLD 行也不携带稳定度信息。
   真正有信息的是少数"有票想买"的行。判读日如果"非全票一致"的行不到 30，H2 按"功效不足"处理。
3. **判读日 30d 标签很少。** 开跑日 + 35 天时，只有开跑头约 5 天的决议（≈190 行，几乎全是 HOLD）满了 30 天窗口。H1 在判读日基本没有功效，
   H2（不需要成熟窗口）才是判读日能出结论的那条。建议用户把 H1 的最终判读推到开跑 + 65 天（约 35 个交易日满窗）再看一次；日期是用户定的，这里只提示。
4. **相邻交易日的 30d 窗口高度重叠、同日标的共享市场环境**，真实独立样本远少于 n，所有 CI 偏窄，符号检验的 p 偏小。
5. **宏观角色只抽了一次**：N 票共用同一份 `macro_view`（和对照臂相同）。宏观采样噪声没有被平均掉——这是有意的（保证两臂输入逐字相同），
   也意味着当天的宏观抽样偏差对所有标的同向。要平均掉它需要每票重跑 Macro（+2 次调用/天），是后续选项，不在本 PR。
6. 单模型（deepseek-v4-flash）、单温度（Quant/Risk 0.2、CIO 0.1）；换模型要重测。
7. 舰队是中性持仓、无 Dreaming/事件/估值输入，结论不能直接外推到带持仓输入的 live。

## 首跑（机制验证，不是证据）

两轮，都在数据拷贝（`INVEST_HOME` = 生产 `memory/*.md` + `db/market_data.db` 的拷贝，用完已删）里，真调 deepseek-v4-flash：

1. **真 CLI 路径**：`--prospective --sealed-arm`，3 个加密标的（周日只有加密有"今天"的 bar；拷贝里先 `backfill_history` 补了今天的 bar）。
   对照臂 3/3、实验臂 3/3 成功，9 票全部 HOLD。
2. **直调 `run_one_day`（同一函数，绕开"必须是今天"的新鲜度闸，今天周日休市）**：5 个标的 A–E（近两个月舰队非 HOLD 较频繁的非加密标的）
   × 8 个交易日（10 月 3 天；9 月 5 天，挑的是生产对照臂当天这 5 个里 ≥3 个非 HOLD 的日子，输入最可能让投票分歧），对照臂 + 实验臂，库按决议日截断。

| 指标（43 个 标的×日，每行 3 票） | 数 |
|---|---|
| 全票一致 | **43/43 = 100%**（95% CI 92–100%） |
| 方向翻转（A4 口径，A4 基线 26.7%） | 0/43 = 0%（95% CI 0–8%） |
| 无多数 → HOLD（split） | 0/43 |
| HOLD 占比：单票 / 聚合后 / 同日同标的对照臂 | 100% / 100% / 100% |
| 逐日方向翻转 / HOLD 配对 / 30d 命中配对 | 不一致对 0 / 0 / 0（30d 成熟可评分 20 对，n<30，红线 #2 不展示命中率） |

**读法**：机制全链路生效（落盘、Input Health 的 `arm/votes/flip/split` 读得回来、续跑跳过、复盘报告生成、n<30 抑制），但**这批输入上没有任何分歧可测**——
对照臂和实验臂逐行相同。这不是"投票没用"的证据，是"舰队现在的输入下委员会饱和在 HOLD"的证据：生产舰队对照臂自 2026-10-08 起
约 330 行里 0 行非 HOLD（10 月全月 670 行里 20 行非 HOLD，且全在 10-07 及之前；2026-07-24 起全部 3,462 行 94% HOLD）。后果见上面的局限 2：H2 很可能判"功效不足"。
聚合规则在分歧输入上的行为由 `tests/test_sealed_arm.py` 用桩 LLM 覆盖（多数 / 平票 / 无多数 / 中位金额 / split 不带规则溯源）。
