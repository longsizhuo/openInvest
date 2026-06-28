# ADR-022：回测记忆穿越走绝对价位指纹,非日期——污染/holdout 分桶纪律

**日期**：2026-06-25
**状态**：accepted
**延续**：ADR-010(param-management)、ADR-020(集中度 lens 默认 OFF)、ADR-021(币种自适应 path-profile)；与 `scripts/backtest_committee.py` + `scripts/holdout_validate.py` + `tests/test_backtest_leak.py` 配套。

## 方法论裁决（已实证）

openInvest 回测的记忆穿越通道**不是日期串**——prompt 里**零日期**,backtest 主动把日期从所有输入里抹掉。脏的是**绝对价位 / 宏观点位指纹**:`Current Price` / `MA120` / `MA250` / `^TNX` / `^VIX` / `DXY` 以及 regime `INPUTS` 行里**未归一化**的 `ma20` / `ma120`,逐字进 prompt。训练到 2024 末的 MiMo(`mimo-v2.5-pro`)拿到 `GC=F` + 价 `1582` + `VIX 13.68` + `DXY 99` 这组指纹,可以反推出"这大约是 2020 年初",从而**解锁记忆里的后市**——它已经知道这之后金价怎么走了。

因此:

- `date <= 2024-12-31` = **污染**桶。只能做行为一致性 / 回归扫描,**绝不报业绩**。
- `date > 2024-12-31 + 95d buffer` = **唯一干净 holdout**,可报预测 / 业绩。

要点:**数据时点本身是干净的**(根级 `get_history_df` cut 扎实,见 §4),脏的**只是模型权重里的记忆**。"回测读了未来数据"是另一回事(那个我们防住了);这里说的是"模型权重见过这段历史"。两者别混。

---

## 正文（8 条 + 证据）

### 1. 泄漏通道是绝对价位 / 宏观点位,不是日期

逐字进 prompt 的指纹字段:

- `utils/exchange_fee.py:330` —— `Current Price: {current_price:.4f}`(绝对价,4 位小数)
- `utils/exchange_fee.py:348` —— `MA120 (Trend): {ma_120:.4f}`
- `utils/exchange_fee.py:350` —— `MA250 (Base): {ma_250:.4f}`
- `utils/exchange_fee.py:387` —— `US 10Y Treasury Yield (^TNX): {tnx_last:.2f}%`
- `utils/exchange_fee.py:390` —— `CBOE Volatility Index (^VIX): {vix_last:.2f}`
- `utils/exchange_fee.py:399` —— `US Dollar Index (DXY, DX-Y.NYB): {dxy_last:.2f}`
- `core/regime.py:155-161` —— `inputs_used` 里 `ma20` / `ma120` 是**未归一化的绝对值**,经 `format_regime_brief` 的 `INPUTS` 行进 transcript。

这组**绝对量**联合起来就是一个时代指纹。任何一个 LLM,只要训练语料覆盖过那段市场史,都能从 `(symbol, price, MA, VIX, TNX, DXY)` 反推大致年代 → 解锁后市。**"prompt 里没有日期所以没穿越"是已证伪的错误论证,别再重复。** 去日期化做得再干净,价位指纹照样泄漏。

### 2. 去日期化无效;归一化只降级不解救

- **去日期化无效**:没有日期可去——backtest 早就不喂日期了。把不存在的东西"再删一遍"零收益。
- **归一化只降级不解救**:就算把 `Current Price` / `MA` 换成相对量(`price/MA250 - 1` 之类),也只是**降低**指纹分辨率,不能消除。原因是**指纹同时是委员会的纪律载荷**——`utils/exchange_fee.py:391` 的 `VIX > 20 indicates fear; VIX < 15 indicates complacency` 直接吃 VIX 的**绝对值**来决定恐惧/自满档位;归一化掉绝对值,这条纪律就废了。指纹与决策信号是**同一份数据**,有**硬天花板**:能洗到的程度 = 能保留多少决策力。
- 推论:即便加一个 `--anonymize` 归一化开关,跑出来的也是**另一个策略**(信号被阉割过的策略),它的业绩**不可升格**为本策略的业绩。anonymize 是研究探针,不是 holdout 的替代品。

### 3. 二分桶是铁律,两桶禁止合并报数

实现已落地在 `scripts/backtest_committee.py`:

- `LLM_TRAINING_CUTOFF = "2024-12-31"`(`:294`)—— 2026-06-25 实测 MiMo 自报训练到 2024 末。
- `FORWARD_BUFFER_DAYS = 95`(`:295`)—— 留够 90d 远期窗口,`verdict_review` 才能用**实际后市收益**评分。
- holdout 筛选 `d > LLM_TRAINING_CUTOFF and d <= hold_end`(`:300`)。

裁决:
- `date <= 2024-12-31` = 污染桶 → 一致性 / 回归扫描,**绝不报业绩**。
- `2024-12-31 < date <= today - 95d` = 干净 holdout(`--holdout`)→ 唯一可信预测 / 业绩。
- `--allow-lookahead`(`:256`)只作上限估计,跑出的数不是干净业绩。

**两桶禁止合并报一个数。** 合并 = 用模型记忆里的"已知后市"稀释干净样本,业绩虚高。

### 4. 数据时点干净,脏的是模型权重;防穿越测试抓不到记忆穿越

根级行情读路径已被 `_patch_tools_to_date` 截断,`tests/test_backtest_leak.py` 守门:`test_root_store_cut_covers_pathprofile_and_fx`(`:33`)断言 `MarketStore.get_history_df` 各 symbol `df.index.max() <= cutoff`;`test_wrapper_get_history_data_cut`(`:25`)守 `ef.get_history_data` 包装层。**这套要保留**——它防的是"回测读到了未来 OHLC"(数据时点穿越),这层确实干净。

但它**结构上抓不到本 ADR 说的记忆穿越**:它只验**行尾时点**(`df.index.max() <= cutoff`),管的是"喂进去的数据截止哪天";它**管不了**"喂进去的过去值本身能反推年代"。`test_backtest_leak` 全绿 **≠** 回测业绩干净。两个正交的"干净":数据干净(测试守) vs 记忆干净(只有 holdout 守)。

### 5. 两个正交的 holdout,别互相冒充

- **参数 holdout**:`scripts/holdout_validate.py`,窗口 `2024-11-18 → 2024-12-31`(`:24`),拿 Optuna best params 在这段上验 `compute_strategy_reward`(`:79`)。它防的是**参数过拟合**。但这整段都 `<= 2024-12-31` → **整段都在污染桶里** → 它**测不了业绩**,只测"参数换个相邻窗口还稳不稳"。
- **记忆 holdout**:`--holdout`(post-cutoff),防的是**记忆穿越**,测的是**业绩**。

当前仓库**只有前者**,且历史上**曾被误当业绩**报过。两者正交,谁也不能替谁:参数 holdout 过 ≠ 业绩可信;记忆 holdout 过 ≠ 参数没过拟合。

### 6. 回测委员会 ≠ live,verdict 分布不可外推 live

backtest 不知道历史某日用户的真实持仓,`scripts/backtest_committee.py:207-211` **硬编码中性 portfolio**(`假设用户持仓中性（无极端集中度）`)。后果:Risk 角色的集中度闸(ADR-020,opt-in 的 concentration lens)在回测里**从不触发**——分母里永远是"中性",没有任何资产能算出极端集中度。所以回测得到的 verdict 分布(BUY/HOLD/TRIM 比例)是**缺了集中度维度**的分布,**不可外推到 live**——live 用户有真实集中度、真实多腿持仓,会走出回测里根本不存在的 TRIM 路径。

### 7. 基准必须是同资产 buy-and-hold,不是余额宝/现金

`core/strategy_metrics.py:119` 的 `vs_benchmark` 已实现正确口径:`alpha_pct`(策略总收益 − 基准总收益)+ `beat_days_pct`(策略日收益高于基准的天数比例),基准曲线是**传入的**(`benchmark_curve`)。报业绩时**必须**喂**同资产 buy-and-hold** 作基准,**不能**用余额宝 / 现金 / 无风险利率。

更深一层的 survivorship 约束:回测的资产池是**今天还活着、还在交易**的资产。因此"openInvest **能选出好资产** / **能产生 alpha**"这类主张是**非法**的(选股能力被幸存者偏差污染)。**唯一合法**的主张是:"**给定某资产**,openInvest 的**择时**能否跑赢该资产 buy-and-hold"——即 alpha 必须**锚定在同一资产上**,只评择时,不评选股。

### 8. 校准参数不进回测 prompt,故其轻 snoop 不污染 backtest holdout

path-profile / 下行口径的校准文本由 `build_reentry_reference`(`core/regime_probability.py:498`)生成。它的**唯一**生产调用在 orchestrator 层:`core/runner/session.py:198` 和 `core/runner/coordinator.py:162`。而 `scripts/backtest_committee.py` **绕过 orchestrator**,直接调原语 `run_committee`(`:217`),**从不**经过那两个调用点 → 校准参考文本(path / defense 那套)**根本不进回测 prompt**。

推论:这些校准参数即便在它们自己的拟合里有**轻度 snoop**(用了接近样本的数据调过),也**不会污染 backtest holdout**——因为回测看不到它们。两个评估面是隔离的:校准参数的 snoop 风险归 §5 的参数 holdout 管,跟 §3 的记忆 holdout 业绩数互不传染。

---

## Validity 威胁表(10 条)

| # | 威胁 | 严重度 | 说明 / 防御 |
|---|---|---|---|
| T1 | **记忆穿越**(绝对价位指纹反推年代解锁后市) | **高** | 只有 `--holdout`(post-cutoff + 95d buffer)能防;污染桶绝不报业绩。本 ADR 主题。 |
| T2 | **幸存者偏差**(资产池只含今天还活的标的) | **高** | 禁止"能选资产 / 产生 alpha"主张;只可主张"给定资产上的择时"。 |
| T3 | **回测委员会 ≠ live**(portfolio 硬编码中性 → 集中度闸从不触发) | **高** | verdict 分布缺集中度维度,不可外推 live。`backtest_committee.py:207-211`。 |
| T4 | **命中率无基准率校正**(裸 hit-rate 没减去 base rate) | 中高 | 报命中率必须同时报同资产 buy-and-hold base rate;裸命中率不解释。 |
| T5 | **reward 锚现金非 buy-hold**(`compute_strategy_reward` 里 cash 不算收益) | 中 | `build_dspy_trainset_v3.py:26` "cash 不算收益";reward 把空仓当 0,等于拿现金作锚 → 偏向"敢满仓"。报业绩改用 `vs_benchmark` 同资产 alpha。 |
| T6 | **参数过拟合 + 两 holdout 冒充** | 中 | 参数 holdout(`holdout_validate.py`,2024-11~12,整段污染)≠ 记忆 holdout;曾被误当业绩。两者正交,别互相替。 |
| T7 | **无成本 + 同收盘前视**(回测没扣手续费/滑点,且用当日收盘价决策又用当日收盘价成交) | 中低 | 上限估计偏乐观;报数注明"未计成本、收盘价撮合"。 |
| T8 | **单样本点估计**(一次回测一个数,无置信区间) | 中 | 报区间 / 多 seed,别拿单点数当结论。 |
| T9 | **path 校准 OOS 轻 snoop**(校准参数拟合时轻度用近样本) | 低 | 隔离:校准文本不进回测 prompt(§8),不污染记忆 holdout 业绩;snoop 风险归参数 holdout 管。 |
| T10 | **warmup / auto_adjust**(指标 warmup 期不足 + yfinance `auto_adjust` 复权口径漂移) | 低 | 留够 warmup;复权口径前后一致即可,不影响相对收益。 |

---

## Consequences

- 报回测业绩**只认** `--holdout` 桶的数;污染桶的数标注"一致性扫描,非业绩",绝不并表。
- `tests/test_backtest_leak.py` 继续守**数据时点**穿越,但文档明确它**不**守记忆穿越——别拿它的绿当业绩护身符。
- 任何 alpha 主张必须**锚定同资产 buy-and-hold**(`vs_benchmark`),且声明"评择时不评选股"。
- 校准参数(path / defense)保持**不进回测 prompt** 的现状(走 orchestrator 而 backtest 绕过),这是 §8 隔离成立的前提;以后谁给 backtest 加 reentry / prob_hint,§8 的隔离失效,本 ADR 要同步改。
