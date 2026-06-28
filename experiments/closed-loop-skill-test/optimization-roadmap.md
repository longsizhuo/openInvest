# 委员会 alpha 优化路线图(经 multi-agent 研究 + 对抗审查)

> 2026-06-27。源:research workflow(数据诊断 + 代码杠杆 + 外部文献 + 系统化技术 → 怀疑派审查)。
> 结论叙事见 `../../docs/wiki/17-closed-loop-skill-backtest.md`。本文件是**可落地的实现清单**。

## 研究的关键发现:bug 改了归因
对抗审查亲查数据:闭环 16 次 TRIM **全负 alloc、全被 `paper_trade_simulator.py` 的 `<=0` 守卫吞成 HOLD**,SELL 执行=0。
→ 初版"−2.58% 来自卖飞 winner"**错**:那段窗口委员会**根本没卖过**。修复 bug(R0)后重测,基线 alpha 从假象 +11.57% → **真实 −8.06%**。真因是 **欠配(现金空置)+ TRIM 真执行后的卖飞**,两头都伤。

## 逐条裁决(KEEP / RISKY / CUT)
| ID | 提案 | 裁决 | 理由 |
|---|---|---|---|
| **R0** | 修 execute_verdict 负 alloc 吞 TRIM + 符号契约 + holdout 正则保负号 | ✅ **KEEP(已做)** | 纯正确性零过拟合;没它整道题没被考过 |
| **R1** | reward 锚 现金→同资产 buy-hold(`core/backtest_reward.py:64-71`) | **KEEP** | 纯目标修正;现金锚下"坐现金跑赢余额宝"恒正分=优化器在奖励防御行为。注意:本身不产 alpha,只是不再把 sweep 往防御拐;换锚后别把新权重当 OOS 真值 |
| **R3-vol** | 确定性 vol-target sizing(`cio_parse.py` clamp 前:`base × clamp(target/realized_vol, 条件) × trend_tilt`) | **KEEP** | 改行为里 OOS 文献最硬、不赌方向、不依赖 LLM 质量的一条;直治欠配/现金空置。**必须**:条件夹断(只在波动到 2 年高/低分位才缩放)+ 封顶,别上无条件版 |
| R3-conviction | 按 confidence 加权 sizing | **RISKY** | README 自承方向命中~25%、confidence 大概率未校准;先用 verdict_review 验单调性再开,否则给噪音加杠杆 |
| R2 | 动量"禁减仓"硬闸(uptrend force-HOLD) | **CUT** | 原数据里是 no-op;内核="牛市永远满仓"=用户点名要避的伪 skill;`p_below<0.3` 阈值会拟合本窗口 |
| R4 | 首仓加速逼买 | **RISKY** | 现金拖累真实该修,但"确认 uptrend 就重仓"是 regime-timing 过拟合 → 降维成 **regime 无关的固定 schedule 加速 DCA** |
| R5 | regime crash 腿 + VIX→size | **CUT/DEFER** | 无真实暴跌窗口证不了;对 2008/2020 调阈值=snooping。ma250 二级确认那点无害可顺带 |
| R6 | 横截面动量 tilt | **CUT** | 仅 3 资产=独立 bet 太少=tilt 向黄金=过拟合 |
| R7 | 加辩论轮/agent | **不做** | 复杂度不产 alpha,别花预算 |

## 最该先做的 3 件(高信心、低过拟合、本窗可验)
1. **R0**(已完成)—— 复跑闭环断言 `n_sells>0`,重新归因 −2.58%(欠配 vs 过减各占多少)。
2. **R1** reward 锚改 buy-hold —— 改读固定键 `buy_hold` 的 `alpha_pct` + `assert "buy_hold" in benchmark_curves`。验证:sweep 收敛方向从"轻仓避险"转"趋势参与"。
3. **R3 仅 vol-target 腿** —— 指标现成(`volatility_annualized`/`ma250`)。验证:同窗 **≥5 跑报 CI**,看 ① Sharpe ② 现金空置时长 ③ buy-hold alpha 是否转正。**必须 A/B vs 傻瓜等额加速 DCA** —— 若不显著优于傻瓜 DCA,说明赢的是"早投进牛市"不是 vol-target,退回更简单的 DCA。

## 铁律
任何"持有/买更多涨过的标的"型改动,必须和傻瓜 buy-hold / 傻瓜 DCA 对照 + 同窗 ≥5 跑报 CI。跑不赢基线的"alpha"就是 beta 换皮。
