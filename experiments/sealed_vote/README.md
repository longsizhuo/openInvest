# sealed_vote — B4 四臂 A/B（对照 / 密封投票 / 辩论 / macro-soft）

**只加实验臂，不改任何 live 裁决**（治理红线：先审计后实验）。live 的 `.committee/`、邮件、日报、对照臂 `.backtest/`、
T2 臂 `.backtest_t2conf/` 一个字节都不变；三个新臂的产物只在数据目录（`memory/.backtest_{sealed,debate,macrosoft}/`、
`memory/.dreams/verdict_review_<臂>.jsonl`，gitignored），仓库里只有代码、文档和匿名汇总。

> 一句话翻译：舰队每天多跑三臂对照（同一批输入上：角色不互相看 + 跑三次投票 / 用 live 日报同款的 4 轮辩论 /
> Macro 偏空时打折而不否决），你收到的裁决不变；65 天后出判读。

## 三个问题

| | 问题 | 对照什么 | 依据 |
|---|---|---|---|
| Q1 | 多次抽样取多数 + 分裂度，比单次抽样更稳、更准吗？ | sealed vs 对照 | A4（[`../verdict_variance/`](../verdict_variance/README.md)）：同输入重跑 3 次方向翻转 26.7%，自报置信度 90/90 恒 0.62 |
| Q2 | 辩论有没有害？ | debate vs 对照 | 评审调研：多轮辩论导致趋同和囤现金。舰队对照臂本来就是 rounds=1，没有辩论的对照就测不出辩论的效果 |
| Q3 | Macro 的 risk_off 否决买入是不是太强？ | macro-soft vs 对照 | [#461](https://github.com/longsizhuo/openInvest/issues/461)：#256 修数据窗口后 Macro 自 10-08 起恒为 risk_off，`cio.md` 规则 2 把所有 Quant 看多的行压成 HOLD |

## 设计

| | 对照臂（现有舰队产物） | sealed | debate | macro-soft |
|---|---|---|---|---|
| 目录 | `.backtest/` | `.backtest_sealed/` | `.backtest_debate/` | `.backtest_macrosoft/` |
| 辩论轮数 | 1（`INVEST_MAX_DEBATE_ROUNDS` 缺省） | 1 | **4**（= `daily_report.py` 的 `max_debate_rounds=4`） | 1 |
| 每标的抽样 | 1 | **N=3 取多数** | 1 | 1 |
| CIO prompt | live | live | live | live，**仅规则 2 换成“打折不否决”** |
| 输入 | 同日同标的：`market_data` / `regime_brief` / 中性持仓 / `macro_view` **逐字相同**（后三臂借对照臂这次跑出来的 `macro_view`；落盘时核对它是否真的在对照臂同日同标的 transcript 里，结果记 `macro_shared`，False 的行复盘时从配对里剔掉并计数） | 同左 | 同左 | 同左 |

每一臂只比对照臂多**一个**变量，所以每个比较都能归因。debate 臂用的是 live 的辩论配置，但输入仍是舰队的中性持仓
（没有持仓上下文，Risk 没有可倾向的内容），所以它回答的是“同样的输入下，4 轮辩论比 1 轮差不差”，不是“live 日报差不差”。

### “密封”在代码里是什么（读 `core/committee/debate.py` 核实）

`max_debate_rounds=1` 时 `for round_idx in range(2, max(2, 1 + 1))` 是空循环：**没有 cross-challenge**。
Round 1 的 Quant / Risk 并行起跑，输入互不相交（Quant 只看行情/估值/情绪，Risk 只看持仓/长期模式）；CIO 只看到两份 Round-1 简报。
“完整辩论历史”和“辩论元信息”两块只在 `len(quant_history) > 1` 时才附加（rounds=1 不会附加）。
一个小毛边（**未改**，改了就动 live）：`CommitteeReport.to_cio_brief()` 无条件打印 `=== ROUND 2 (cross-challenge 后的调整) ===`
标题，rounds=1 时它下面是两段空的“调整”，CIO 看到的是空标题。

### sealed 臂的聚合规则（`jobs/review_calc.py:aggregate_votes`，纯函数；舰队脚本 re-export，复盘侧的一致度分档用同一份，多数判定只定义一次）

- **verdict**：严格多数（票数 > N/2，N=3 即 ≥2 票相同）；没有多数（N=3 三者各异、N=4 的 2:2 …）→ `HOLD` 并标 `split`。
- **confidence** := 最大票占比（N=3：1.0 / 0.67；三者各异 0.33）。
- **alloc** := 多数票那几次建议金额的**中位数**；HOLD（含 split）恒 0。
- **flip**：N 次的方向类（`EXPECTED_DIRECTION`，UNCLEAR=none）不全相同——与 A4 的翻转口径同一把尺子，数字可直接比。
- 落盘的 transcript 正文取**代表票**（多数票里最早的一次；split 取第 1 次，并在 CIO 备忘录末尾注明“票数分裂 → HOLD”）；
  `rewritten` / `forced_hold` 也是代表票的（split 的 HOLD 是投票结果，不是规则改写）。
- Input Health 块多：`arm="sealed_n3"`、`votes=[{verdict,confidence,alloc}×N]`、`flip`、`split`、`macro_signal`、`macro_strength`、`macro_shared`、`final_round`。
- 任一次抽样抛异常 → 该标的整体失败、**不落盘**（下次续跑重来），保证臂内每一行都恰有 N 票；同日已落盘的标的跳过（断点续跑同口径）。

### debate 臂

单次抽样、`run_committee(max_debate_rounds=4)`：Quant / Risk 最多 3 轮 cross-challenge，连续两轮意见稳定即提前收敛，CIO 看完整辩论历史。
Input Health 多 `arm="debate_r4"`、`macro_signal` / `macro_strength` / `macro_shared`、`final_round`、`converged`（报告里出“实际跑了几轮 / 提前收敛率”）。

### macro-soft 臂（#461 的证据臂）

CIO 规则 2（`cio.md:19`）原文只有一句 `2. **Quant vs Macro 分歧**: 看 Risk Officer 倒向哪边`。变体在这句后面**只加一段**：

> **例外——Macro 偏空但不否决**：当 Macro SIGNAL=risk_off 且 Macro STRENGTH < 8、Quant 看多、Risk Officer 不是 high_risk 时，Macro 的
> risk_off **不构成否决**：允许给 ACCUMULATE（仍不允许 BUY），SUGGESTED_ALLOC 取你按其余规则本来会给的金额 × (10 − Macro STRENGTH) / 10，
> 并在备忘录里写明折扣系数；Macro STRENGTH ≥ 8 的 risk_off 仍然否决（HOLD）。

Macro 的 STRENGTH 是 0–10；`risk_off` 的实际取值 5–7，所以现状下这条例外几乎总是生效、≥8 的否决几乎不触发。

- **开关**：和 T2 臂同一种方式——运行期 `patch("openinvest.core.committee.debate.build_cio_prompt", build_cio_prompt_macro_soft)`，
  只在 `--macro-soft-arm` 的 `with _macro_soft_arm():` 里生效；**生产代码零改动**，live 委员会不 import 本脚本，live prompt 逐字节不变
  （`tests/test_sealed_arm.py` 用 sha256 守：开关内外 live CIO prompt 相同；变体 = live + 规则 2 后插入的一段，别无二致）。
  不另放 `cio.macro_soft.md` / 不加 `INVEST_CIO_PROMPT_VARIANT`：整份 prompt 副本会随 `cio.md` 漂移，而锚点替换在 `cio.md` 改了规则 2
  措辞时**直接报错**（花 LLM 钱之前先 preflight，失败只跳过这一臂、其余臂照跑、退出码非 0）。
- 折扣由 CIO 在备忘录里按规则执行（不是确定性后处理）：首跑里 CIO 的备忘录写出“折扣系数 (10 − 6)/10 = 0.4，基准 45000 × 0.4 = 18000”。
  H3 用名义金额加权的比率，折扣只影响权重；若进入 live 提案，折扣必须落成 `cio_parse` 里的确定性后处理，那是另一个 PR。
- Input Health 多 `arm="macrosoft_r1"`、`macro_signal`、`macro_strength`、`macro_shared`（报告里数“仍被 ≥8 否决的行”）。

## 怎么开 / 停 / 跑

```bash
# 手动跑一天（对照臂 + 三个新臂；T2 臂可同开）
BACKTEST_WORKERS=25 uv run python -m scripts.backtest_committee --prospective \
    --sealed-arm --debate-arm --macro-soft-arm --assets "$SYMS"
#   --sealed-n 3     sealed 臂每标的抽样次数（默认 3，最少 3）。开跑后别改：同目录混不同 N 的行不可比
#   --limit N        成本闸，只跑前 N 个标的
```

三个 flag 彼此独立，顺序固定 sealed → debate → macro-soft（macro-soft 的锚点失效不会拦住前两臂）；都只能和 `--prospective` 同用。
**开任一试跑臂时 `INVEST_MAX_DEBATE_ROUNDS` 必须未设或为 1**，否则花钱前直接 `SystemExit`：对照臂与 macro-soft 臂读它，sealed 固定 1 轮、debate 固定 4 轮，
env 一旦不是 1（Optuna 实验遗留之类），臂与臂之间会悄悄多出“轮数”这个变量，报告完全看不出来。
调度和 T2 臂一样：舰队的 crontab 行（不在仓库里，见 [`../paper_fleet/README.md`](../paper_fleet/README.md)）在 `--prospective` 后加 flag 即开，删掉即停。
**合并后等生产 checkout 自动 pull 了再改 crontab**（旧代码不认 flag，argparse 报错会让整条舰队 cron 失败）。

复盘不用另配 cron：`verdict_review` 每日 job（02:00 Asia/Shanghai）尾巴调 `run_arms()`，写每臂一份 `memory/.dreams/verdict_review_<臂>.jsonl`
（`source="arm:<臂>"`）和一份 `docs/verdict_accuracy_arms.md`（臂一览 → 四臂并排 → 每臂 vs 对照的配对检验 → debate 轮数 → H3；只含聚合，无 symbol，无金额）。
这些**不进** live 命中率页、`verdict_review.jsonl`、`confidence_lookup.json`、公开 `accuracy_summary.json`
（`tests/test_arm_review.py` 逐项守：有无实验臂目录，live 产物逐项相同）。手动：
`INVEST_HOME=<数据目录> uv run python -c "from openinvest.jobs.verdict_review import run_arms; print(run_arms())"`。

## 成本（按 2026-10-15/16 舰队实测的 `llm_usage.jsonl` 单价口径，deepseek-v4-flash，含 KVCache 命中价）

一次 rounds=1 委员会 = Quant + Risk + CIO 共 3 次调用，均值约 ¥0.0041。首跑实测的比例：debate（rounds=4，全部在第 3 轮收敛）
每标的 7 次调用、花费 ≈ 对照的 2.3 倍；macro-soft 3 次调用、≈ 对照的 1.04 倍（prompt 多一段、备忘录多写折扣）。debate 最坏 9 次调用/标的。

| 每天 | 交易日（≈46 个标的） | 周末日（3 个加密） |
|---|---|---|
| 现有舰队（对照 + T2） | ≈278 次调用，¥0.40 | ≈18 次，¥0.03 |
| + sealed（N=3，3 次/标的 × 3） | +414 次，≈ ¥0.57 | +27 次，≈ ¥0.04 |
| + debate（≈7 次/标的，最坏 9） | +322 次（最多 +414），≈ ¥0.44 | +21 次，≈ ¥0.03 |
| + macro-soft | +138 次，≈ ¥0.20 | +9 次，≈ ¥0.01 |
| **三臂合计新增** | **+874 次，≈ ¥1.20** | **+57 次，≈ ¥0.08** |

约 **+¥27 / 月**（22 个交易日 × 1.20 + 8–9 个周末日 × 0.08），总舰队 ≈ ¥9 → ¥36 / 月。macro 都借对照臂的，不重跑。
标的间并发不变（`BACKTEST_WORKERS`），同一标的的抽样 / 辩论轮次串行，所以 sealed 与 debate 臂墙钟各约为对照臂的 3 倍、同时在飞的请求数不增。
首跑实花：这一轮 ¥0.45（358 次调用，含成本探针），上一轮 sealed 臂首跑 ¥0.62。

## 判据（开跑前冻结；看到数据后不改 N、聚合规则、变体文字、判据）

**判读日期 = 开跑日 + 65 天**（开跑日 = 三个臂目录里最早的日期目录）。65 天 = 30d 窗口要满 + 约 35 个交易日的成熟样本；
开跑 + 35 天时只有头约 5 天的决议（≈190 行）满了 30 天窗口，不够判 H1 / H3。判读由 AI 起草、用户签字；
本表是“要不要提 live 切换提案”的门槛，**不是**切换本身——切换是另一个 PR，要附“一句话信号翻译 + 确定性回算差异报告”，要用户签字。

过其一才提案（同一份报告 `docs/verdict_accuracy_arms.md` 里读）：

- **H1（sealed 准）**：整侧 30d「提升」的 95% CI 下界 > 0。提升 = 命中率 − 同尺子基率；CI = 命中率 Wilson 区间减基率（基率当常数，
  与 live 命中率页同口径）；30d 成熟可评分 n ≥ 30 才判。
- **H2（sealed 稳）**：配对（同日同标的）的**逐日方向翻转**（同一标的相邻决议日方向类不同）sealed 显著少于对照臂，**且** HOLD 占比不更高。
  “显著” = 只看两臂结果不同的那些对，精确符号检验双侧 p < 0.05；“HOLD 占比不更高” = HOLD 配对里“仅 sealed 为 HOLD”没有显著多于“仅对照为 HOLD”。
  **功效闸**：判读日 sealed 里“非全票一致”的行 < 30 → 判“功效不足”，既不算通过也不算否定。
- **H3（macro-soft 值）**：被“解锁”的买入——macro-soft 给了 BUY/ACCUMULATE、对照臂同日同标的没有买——成熟（30d）行 n ≥ 30，且
  相对基准的 30d 超额均值 95% CI 下界 > 0（超额取对照臂同日同标的行；verdict 无关，两臂同一标的同一窗口本就相同），
  且这些行的 30d 照做净收益率（扣往返成本、名义加权、对比留现金，复用 #442）> 0。
- **debate 臂**不设“通过”门槛，它是对照：回答“辩论有没有害”——与对照臂比 HOLD 占比（囤现金）、逐日翻转（趋同 / 摇摆）、提升与反事实账本。
  若 debate 臂显著更 HOLD 或不更准，是 live 日报 4 轮配置的证据，走另一个治理流程。
- 都没过 → 关臂（crontab 删 flag），把 negative result 写进 `docs/wiki/12-verification.md`，不论好坏都公布。

怎么读 H2：对照臂每天只有 1 票，没有臂内重复可比，所以“稳定度”只能用逐日翻转在两臂间同口径比较——输入的真实变化两臂共享（同日同标的），
差出来的部分是抽样噪声。sealed 臂内的“全票一致率 / 方向翻转率”（A4 口径，A4 基线 26.7%）是描述性的：它量的是**单票**的不稳，
不是聚合后的不稳，不能直接当 H2。分裂度是否带信息，看报告里“一致度 vs 30d 命中”那张表（n<30 不出数字）。
H1 / H2 / H3 是“多选一通过”，名义误报率随之放大，所以这只是进入提案的门槛，不是证据终点。

## 已知局限（读结果前先看）

1. **debate 臂不等于 live 日报。** 它用 live 的辩论配置（4 轮），但输入是舰队的中性持仓、无 Dreaming / 事件 / 估值 / 情绪——
   Risk 没有持仓上下文可倾向，辩论可以交换的信息也比 live 少。
2. **sealed 臂的功效取决于对照臂有没有分歧可测。** 2026-10-08 起 Macro 恒为 risk_off，舰队对照臂约 330 行里 0 行非 HOLD（#461）；
   首跑里 sealed 臂 13/13 行全票一致 HOLD。macro-soft 臂正是为了在这个饱和下拿到有分歧的行——但 H1 / H2 在 Macro 不回到 neutral 之前
   很可能仍是“功效不足”。
3. **macro-soft 臂只动了规则 2**：CIO 规则里其他提到 risk_off 的地方（SELL / TRIM 的触发、`cio.md:100` 的浮亏规则）没动；
   折扣是 CIO 按规则自行执行的，不是确定性的。H3 看的是“解锁出来的买入事后值不值”，不是“这条规则对不对”的最终答案。
4. **相邻交易日的 30d 窗口高度重叠、同日标的共享市场环境**，真实独立样本远少于 n，所有 CI 偏窄，符号检验的 p 偏小。
   H3 的超额用基准（`portfolio.benchmark_symbol`，默认沪深 300）算，对债券 / 黄金 / 美股标的基准并不贴切——所以 H3 同时要求照做净收益率 > 0（对比留现金）。
5. **宏观角色只抽了一次**：各臂共用同一份 `macro_view`（和对照臂相同）。宏观采样噪声没有被平均掉——这是有意的（保证各臂输入逐字相同）。
   “借对照臂的 macro_view”只在对照臂这次真跑了 Macro 时成立：对照臂当天整天被续跑跳过、或中途失败重跑（先落盘的标的用的是上一次的 Macro）时，
   臂拿到的是另一份。所以每行落盘时核对一次（`macro_shared`），不是同一份的行在复盘里剔掉并在“臂一览”里计数。
6. 单模型（deepseek-v4-flash）、单温度（Quant/Risk 0.2、CIO 0.1）；换模型要重测。舰队是中性持仓，结论不能直接外推到带持仓输入的 live。
7. sealed 与对照臂的“密封”相同（都是 rounds=1）；live 现状是日报 4 轮、Web 默认 4、事件重跑 2、Direct CLI 1。

## 首跑（机制验证，不是证据）

数据拷贝里真调 deepseek-v4-flash，两轮：

1. **真 CLI 路径**：`--prospective --sealed-arm --debate-arm --macro-soft-arm`，3 个加密标的（周日只有加密有“今天”的 bar；拷贝里先 `backfill_history` 补了今天的 bar）。
   对照 + 三臂各 3/3 成功。
2. **直调 `run_one_day`（同一函数，绕开“必须是今天”的新鲜度闸，今天周日休市）**：5 个标的 A–E × 2 个交易日（挑生产对照臂里
   Quant 看多且 Macro risk_off 的日子，输入最可能让 macro-soft 解锁买入），四臂各一次。

| 13 个 标的×日（每臂同一批输入） | 对照 | sealed | debate | macro-soft |
|---|---|---|---|---|
| HOLD 占比 | 100% | 100% | 92% | **85%** |
| 非 HOLD 行 | 0 | 0 | 1 | **2**（均为 ACCUMULATE） |
| 备注 | | 13/13 全票一致 HOLD | 13/13 在第 3 轮收敛 | 2 个 ACCUMULATE 的备忘录写明折扣系数 (10−6)/10=0.4 |

**读法**：变体生效——macro-soft 臂在 Macro risk_off（STRENGTH 6）下出现了对照臂没有的 ACCUMULATE，CIO 的备忘录逐字执行了“不否决 + 折扣”，
其余三臂与 #461 的现象一致（Quant 看多的行仍被 Macro 否决）。13 行不足以说明方向对不对：这些日期 30d 窗口还没满，报告里 30d 列全部按红线 #2 抹掉，
H3 显示“成熟解锁行不足”。debate 臂用的是 live 同款 CIO prompt，它那 1 个 ACCUMULATE 是 4 轮辩论的结果，样本太小，不能读成方向。
