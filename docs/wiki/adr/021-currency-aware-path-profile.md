# ADR-021：币种自适应 path-profile（逐日对齐汇率折算 / currency-aware）

**日期**：2026-06-25
**状态**：accepted
**延续**：ADR-020(集中度默认关) 无直接关系；与 path-profile 体系（`core/regime_probability.py`）+ 历史回填（`scripts/backfill_history.py`）配套。

## Context

委员会的 path-profile（卖出后/持有的 forward 路径分布，喂给 CIO 出 TRIM 买回点 + 下行口径）一直在**资产的报价币种**上算：GC=F 报美元/盎司,所以 regime 分类、forward 收益分布、下行分位全是 **USD 黄金**的。CNY 转换只在**显示层**做（浙商积存金 ¥/克）。

但用户实际持有的是 **¥/克 计价的浙商积存金** —— 他承受的风险是 **XAU/CNY**(= XAU/USD × USDCNY)。FX 会改变图景：金价下跌的窗口里人民币常走强,放大 CNY 持有者的损失。实测(downtrend)：

| 90d | p_below | 20 分位下行 |
|---|---|---|
| USD 口径(委员会原来用的) | 0.37 | −5.1% |
| **CNY 口径(用户真实)** | **0.40** | **−5.9%** |

→ **USD 口径系统性低估了 CNY 持有者的下行。**

**为什么不能简单"直接建 XAU/CNY 序列再跑"**：人民币 2005 年才真正浮动(USDCNY 2001 起、2005 前盯死 8.28),所以连续的 XAU/CNY 市场史只有 ~20 年 → downtrend 90d 独立样本只有 ~5(`effective_n=5`),既不足又偏(那点样本全来自 CNY 金牛市)。直接序列法解决不了。

## Decision：逐日对齐汇率折算（date-aligned FX conversion）合成持仓币种分布

不去等那个稀缺的"合并历史",而是拿一条**样本厚**的本币腿,逐日按**当时实际发生的汇率**折算：

- **本币黄金腿**：XAU/USD downtrend forward 分布 —— 回填后 57 年(ADR-020 / backfill_history),90d `effective_n=16`。够厚,是分布主体。
- **汇率腿**：USDCNY forward 收益 —— MarketStore 里 2001+。**不另抽样**,按日期对齐到本币腿。

合成：对每个 regime 日 `d`,取**同一日历窗内**实际发生的汇率远期收益,逐点
`r_cny(d) = (1 + r_usd(d)) × (1 + r_fx(d)) − 1`,堆成 CNY 口径分布。

### 为什么逐日对齐而非"两两随机组合"（MC 卷积）

早期实现用蒙特卡洛把两袋样本**独立**两两配对(20 万次随机抽 r_usd × 随机抽 r_fx)。code review 揪出三个缺陷：

1. **两腿跨度不一致**：汇率腿当时用 `shift(-d)`(交易日),本币腿用日历日 forward —— 90d 窗里 FX 实际跨了 ~126 日历日,卷出来的下行系统性偏深。
2. **无视 point-in-time**：汇率腿读全量历史,与本币腿的 `asof` 截点不一致 → walk-forward 会前视。
3. **独立配对抹平共动**：金跌时人民币常走强(放大 CNY 持有者亏损)正是本 ADR 的动机,独立随机配对恰好把这个相关性平均掉 —— 在它最想预警的相关回撤里反而低估。

改为**逐日对齐**(按日期内连接本币腿与汇率腿、逐点相乘)一次修掉三者：同一 `searchsorted` 日历日 → 跨度一致;两腿同 `asof` 过滤 → 零前视;用**实际同窗共动**而非随机配对 → 保留"金跌 × CNY 强"的真实尾部。无随机性 → 天然确定性,无需种子。

样本数 = 本币腿 regime 日 ∩ 有汇率远期收益的日 ≈ 本币腿样本(FX 覆盖全程)。瓶颈仍是本币腿,靠 57 年回填撑厚 —— **不靠"16×68 笛卡尔积"虚增独立样本**(那是独立性假设的产物,本就不成立)。

## 实现（opt-in，默认 USD 行为零改动）

- `get_path_profile(asset, regime, …, convert_ccy=None)`：默认 `None` → 完全不变。设 `convert_ccy="CNY"` 时,**主 windows 仍是报价币种**(文本/价位用),另把逐日对齐折算的持仓币种分布挂到 `currency_overlay`(同一帧算完,**单次行情加载** —— 不再像早期那样二次 `get_path_profile` 重读)。条件 + 无条件分布都换算,使 `calibrate_profile` 的收缩留在同币种内。`asof` 同时下传到汇率腿,零前视。
- `effective_n` **仍取本币(USD)腿的值**(瓶颈腿);转换后另存 `fx_aligned_n`(对齐交集样本数)作元信息。
- `quote_currency_iso(asset)` 覆盖主流交易所后缀(.AX/.HK/.SS/.SZ/.L/.T/.TO/.PA…+ 加密 -USD/-EUR);未知后缀按 USD 兜底,但 `currency_overlay` 仅在汇率序列真实存在时才挂 —— 漏标也不会凭空臆造一条假折算。
- `convert_ccy_for(asset, holding_cost_ccy)`：持仓计价币种 ≠ 资产报价币种(`quote_currency_iso`)时返回需转换币种,否则 None。例:GC=F(报 USD)+ 浙商积存金(cost_currency CNY)→ "CNY";510300.SS(报 CNY)+ CNY 持仓 → None。
- 接线:`core/runner/session.py`(Direct/Web/Cron)和 `core/runner/coordinator.py`(Coordinator)都按持仓 `cost_currency` 自动传 `convert_ccy`(查持仓失败 → 退回本币,不影响 reentry 主体)。
- 输出:结构化 `path_profile` 挂 `currency_overlay`(持仓币种分布);reentry 文本末尾附一行 ⚠ 持仓币种口径下行(对比**报价币种**口径,如 USD/AUD/HKD —— 不写死 USD)给 CIO。

## v1 近似（明确记录，后续可精化）

1. **汇率腿的远期收益本身未再按 gold-regime 二次过滤**：逐日对齐已隐含挑出"本 regime 那些日子的实际 FX 共动",但 FX 序列没再单独按 regime 条件化 —— CNY 管理浮动、波动远小于黄金,二次条件化会把样本打薄。留作 refinement。（早期版本"独立 MC 配对抹平共动"的缺陷已被逐日对齐修掉,不再是近似项。）
2. **路径"形状"(何时见底/回踩)仍按本币**,只换算终端风险数字 —— 形状由黄金主导,FX 只是终端小修正。
3. **价位仍为资产报价币种**(USD/oz),与委员会其余 brief(Quant 的 MA、Risk 等)同币种,避免单 brief 串币种;持仓币种信息以"百分比口径 + ⚠ 提示"形式给出。

## Consequences

- CNY 计价持仓(浙商积存金)的委员会现在看到**真实币种的下行**(更深),USD 口径不再低估。
- 样本靠 57 年本币腿撑厚(90d eff_n=16),逐日对齐保留金汇真实共动,绕开"长 XAU/CNY 历史不存在"。
- 完全 opt-in:非持仓币种错配的资产(510300.SS 等 CNY 报价 + CNY 持仓、纯 USD 资产)`convert_ccy=None`,USD/本币行为零改动。
- 配套:本币腿的厚度依赖 ADR-020/backfill_history 的长历史回填(USD 黄金 57 年);若本币腿仍薄,可叠加 lever-1 收缩(`calibrate_profile`,默认禁用待 fit)。
