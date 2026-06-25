# ADR-021：币种自适应 path-profile（汇率卷积 / currency-aware）

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

## Decision：汇率卷积（FX convolution）合成持仓币种分布

不去等那个稀缺的"合并历史",而是用两条**各自样本都厚**的腿拼出来：

- **本币黄金腿**：XAU/USD downtrend forward 分布 —— 回填后 57 年(ADR-020 / backfill_history),90d `effective_n=16`。
- **汇率腿**：USDCNY forward 分布 —— MarketStore 里 2001+,90d `effective_n≈68`。

合成：`r_cny = (1 + r_usd) × (1 + r_fx) − 1`。

### 什么是"卷积"（给非数学读者）

卷积 = **把两件独立的事的可能结果两两组合,得到合在一起的结果分布**。做法(蒙特卡洛)：

1. 从"美元金价袋"随机抽一个结果(如 90 天 −4%);
2. 从"汇率袋"随机抽一个结果(如同期人民币走强 USDCNY −1%);
3. 合起来算这一情形的 CNY 金价:`(1−0.04)×(1−0.01)−1 = −4.96%`;
4. **重复 20 万次**(每次随机配对),堆成一个新分布 = "CNY 黄金会怎么走"。

为什么解决样本不足:16 种金价情形 × 68 种汇率情形,两两组合**远多于**任何单独一袋,更远多于那 5 个直接样本。就像求"两个骰子之和"的分布不用真掷很多次那对骰子,把 A 的点数和 B 的点数两两相加即可 —— 卷积就是这个"两两组合"。

固定随机种子(`_CONVOLUTION_SEED`)→ 确定性,可缓存 / walk-forward 复算。

## 实现（opt-in，默认 USD 行为零改动）

- `get_path_profile(asset, regime, …, convert_ccy=None)`：默认 `None` → 完全不变。设 `convert_ccy="CNY"` 时,对每个 window 取 USD 黄金的 fwd 原始样本,与 `USD{ccy}=X`(读 MarketStore)的远期收益 MC 卷积,重算终端分布(median/p_below/p_down/p10/p90/downside)。条件分布 + 无条件分布都换算,使 `calibrate_profile` 的收缩留在同币种内。
- `effective_n` **仍取本币(USD)腿的值**(瓶颈腿),不被卷积虚抬;另存 `fx_effective_n` 作元信息。
- `convert_ccy_for(asset, holding_cost_ccy)`：持仓计价币种 ≠ 资产报价币种(`quote_currency_iso`)时返回需转换币种,否则 None。例:GC=F(报 USD)+ 浙商积存金(cost_currency CNY)→ "CNY";510300.SS(报 CNY)+ CNY 持仓 → None。
- 接线:`core/runner/session.py`(Direct/Web/Cron)和 `core/runner/coordinator.py`(Coordinator)都按持仓 `cost_currency` 自动传 `convert_ccy`(查持仓失败 → 退回本币,不影响 reentry 主体)。
- 输出:结构化 `path_profile` 挂 `currency_overlay`(持仓币种分布);reentry 文本末尾附一行 ⚠ 持仓币种口径下行(对比 USD 口径)给 CIO。

## v1 近似（明确记录，后续可精化）

1. **汇率腿用无条件分布**(全 2001-2026 USDCNY 远期),不按 gold-regime 条件 —— CNY 是管理浮动、波动远小于黄金,条件化又会把 FX 样本打薄。配对/条件采样留作 refinement。
2. **MC 独立配对** r_usd ⊥ r_fx —— 金价(USD)与 USDCNY 相关性低(人民币管理浮动),独立卷积是可辩护近似。
3. **路径"形状"(何时见底/回踩)仍按本币**,只换算终端风险数字 —— 形状由黄金主导,FX 只是终端小修正。
4. **价位仍为资产报价币种**(USD/oz),与委员会其余 brief(Quant 的 MA、Risk 等)同币种,避免单 brief 串币种;持仓币种信息以"百分比口径 + ⚠ 提示"形式给出。

## Consequences

- CNY 计价持仓(浙商积存金)的委员会现在看到**真实币种的下行**(更深),USD 口径不再低估。
- 样本充足(USD 16 ⊗ FX 68),绕开"长 XAU/CNY 历史不存在"。
- 完全 opt-in:非持仓币种错配的资产(510300.SS 等 CNY 报价 + CNY 持仓、纯 USD 资产)`convert_ccy=None`,USD/本币行为零改动。
- 配套:本币腿的厚度依赖 ADR-020/backfill_history 的长历史回填(USD 黄金 57 年);若本币腿仍薄,可叠加 lever-1 收缩(`calibrate_profile`,默认禁用待 fit)。
