# cooldown-demo：只给正反理由、不给结论的委员会演示

参赛用的公开演示（“投资冷静期”）：用户说出想买或想卖的东西，委员会照常辩论，
但对外**只给支持的理由 / 反对的理由 / 分歧最大的地方**，不给买卖结论、金额、置信度。
未经许可向公众提供金融领域的确定性结论有合规风险，比赛规则也明令禁止。

**生产代码零改动**，这里只是外面接的一层：

1. `run_committee_session` 跑完整委员会（顾问模式：不读真实持仓、不落盘）。CIO 照跑，
   它的裁决在进程内直接丢弃——代价是每次多一次 LLM 调用，换来的是不用在
   MCP / CLI / web_api 各处加闸。
2. 书记员（`scripts/scribe.md`）只读三个角色的辩论和确定性事实（`to_cio_brief()`，
   不含 CIO memo），整理成三段 Markdown，末尾由代码固定追加免责声明。
3. 逐字闸 `find_verdict_language`：裁决词、“建议/应该/适合 + 买卖加减仓”句式、金额、仓位、
   目标价、支撑阻力位、概率数字。查两个视图——返回给用户的那一份（NFKC + 繁→简），
   以及模拟 Markdown 渲染后人读到的字串（去空白和所有 Markdown 语法、组合附加符，
   西里尔/希腊同形字折回拉丁）；HTML 标签、实体、格式控制字符直接算命中。命中就带着
   命中片段重写一次，再命中整份拦下（fail closed，不做局部删改）。
4. `debate_summary()` 的返回值按白名单构造：`{symbol, debate_summary}` 或 `{status, error}`。

## 跑

必须顾问模式 + 独立 `INVEST_HOME`（不在顾问模式直接拒绝：否则 CIO 裁决和路径快照会落进真实账本）：

```bash
INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 \
    uv run python experiments/cooldown-demo/scripts/cooldown.py 510300.SS
```

**公开服务只能暴露 `debate_summary()`**，不能直接暴露 openinvest 的 MCP / web_api
（`run_committee` / `explain_decision` 等出口都带裁决）。

## 测试

不在 CI 里（CI 只跑 `tests/`），改代码前后手动跑：

```bash
uv run pytest experiments/cooldown-demo/scripts/ -q
```

闸的正例（含 13 种绕过写法）、反例（正常分析句不误伤）、重写一次、二次命中拦下、LLM
不可用拦下、非顾问模式拒绝、出口白名单 + 书记员看不到 CIO。

## 实测（2026-10）

真实 DeepSeek 跑了 A 股 ETF 和黄金共 6 次（3 次走本目录的外壳，3 次是早期写进委员会内部的
同一套逻辑）：三段结构完整、带免责声明、无落盘；最后两次确认返回原文、中文标点不被改成半角。
场外基金要等 `FUND:` 历史行情（openInvest#349）合进去后再跑。

- 早期两次漏过支撑/阻力价位（“下方有 4.30 支撑参考”“4.30 被其视为下方支撑”“MA120 4.76 为上方阻力”），
  闸放宽成“同一分句里数字和支撑/阻力相距 12 字以内”，三句都进了用例。
- 放宽后的两次实跑：第一版都写了支撑价位、被闸拦下，带着命中片段重写后第二版通过——
  重写路径在真实 LLM 上走得通。Quant 几乎每次都谈支撑位，所以 prompt 里点名不转述，
  以减少重写（这条 prompt 改动之后还没实跑过）。

## 上限

- 关键词闸挡不住同义改写。要更稳：加一道 LLM 二审，或者演示前端按**纯文本**渲染
  （从根上消掉 Markdown 渲染差异）。
- 书记员 prompt 和闸只覆盖中文。
- 顾问模式下群友可加新闻源，新闻经 Macro 进书记员上下文——注入入口是现实的，闸就是为它设的。
