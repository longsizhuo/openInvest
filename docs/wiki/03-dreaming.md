# Dreaming 记忆整合

> LLM 没有跨会话记忆。Dreaming 是 OpenClaw 风格的三阶段后台整合，
> 让"6 个月前因为过度集中持仓被警告过"这件事，今天的 Risk Officer 仍然知道。

[← 02-agents](02-agents.md) · [Wiki 索引](README.md) · [04-execution-paths →](04-execution-paths.md)

---

## 问题

每次跑委员会，4 个 LLM 角色都是"全新出生"：

- Quant 不知道你上周在 RSI>70 时加仓过
- Risk Officer 不知道你 6 个月前被警告过集中度
- CIO 不知道哪些 verdict 历史命中率低

**结果**：永远在重复犯同样的错。

## 方案：三阶段后台整合

仿 [OpenClaw Dreaming](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e)，每天凌晨 03:00 跑：

```
                              ┌─────────────────┐
        近 90 天交易 + 行情 →  │  Light Sleep    │  → short-term-recall.json
                              │  (摄入信号)      │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │  REM Sleep      │  → candidates.json
                              │  (找重复模式)    │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
        阈值门 score≥0.8       │  Deep Sleep     │  → insights/*.md
              count≥3      →  │  (固化长期模式)  │     + MEMORY.md 索引
                              └─────────────────┘
                                       │
                                       ▼
                          第二天的 Risk Officer
                          context 注入 insights
```

**核心思想**：阈值门保证只有**反复出现且证据充分**的模式才进入长期记忆，避免 LLM 把噪音当信号。

---

## 1. Light Sleep（摄入信号）

源：`jobs/dreaming.py:_light_sleep`

**输入**：
- `memory/portfolio_history.jsonl` 最近 `LOOKBACK_DAYS=90` 天的所有交易
- `CONTEXT_SYMBOLS` 列表里所有 symbol 的 2 年行情（用于事后归因）

**做什么**：
- 把每笔交易包装成"信号事件"（含当时 RSI / 价格分位 / regime / 持仓状态）
- 算每笔交易事后 7d / 30d / 90d 收益
- 输出 `.dreams/short-term-recall.json`

**为什么不直接 LLM 总结**：
- 90 天可能上百笔交易，全喂 LLM 太贵
- 这一阶段是**机械摄入**，不要 LLM 主观解读

---

## 2. REM Sleep（找重复模式）

源：`jobs/dreaming.py:_rem_sleep`

**输入**：`.dreams/short-term-recall.json`

**做什么**：
- 跨时间分组（按 regime / asset / verdict 类型）
- 找出"出现 ≥ 3 次的模式"作为 candidate
- 例如：
  - "在 RSI>70 时 BUY → 7 天后平均 -3.2%（5/7 次）"
  - "在持仓集中度 >70% 时未减仓 → 30 天后平均 +1.1% 但最大回撤 -18%"

**LLM 介入**：
这阶段会调一次 LLM 让它给候选模式起个**人话名字** + 一句话解释。
但**所有数值统计都是确定性算出，LLM 不能改**。

**输出**：`.dreams/candidates.json`

---

## 3. Deep Sleep（固化）

源：`jobs/dreaming.py:_deep_sleep`

**阈值门**（关键设计）：

```python
INSIGHT_THRESHOLDS = {
    "score": 0.8,    # candidate 综合得分 ≥ 0.8 才能过
    "count": 3,      # 该模式至少出现过 3 次
    "consistency": 0.7,  # 命中方向一致率 ≥ 70%
}
```

**为什么这么严**：
- 防 LLM 在噪音上编故事
- 长期记忆一旦写入会影响后续每次决策，宁可漏不可错
- 实证：90 天数据通常只有 0-3 条能过门

**通过门的模式**：
- 写入 `memory/insights/*.md`（一条一文件）
- 更新 `memory/MEMORY.md` 索引（让 Risk Officer 注入时可发现）

---

## 4. 怎么被 Risk Officer 看到

`core/committee_runner.py:run_committee_for_symbol` 在调 Risk 前：

```python
# 拉所有 insights，注入 risk prompt
prior_insights = "\n".join(
    p.read_text() for p in (Path("memory/insights")).glob("*.md")
)

risk_input = f"""
# 用户当前持仓: {portfolio_summary}
# 长期行为模式（Dreaming）:
{prior_insights or '(暂无)'}
请评估风险。
"""
```

**Risk 看到的 insight 例子**：
```markdown
---
name: high_concentration_pattern
score: 0.87
count: 4
consistency: 0.75
generated_at: 2026-04-15
---
# 高集中度持仓未减仓的历史代价

过去 90 天观察到 4 次：当 NDQ.AX 集中度 > 70% 时，用户保持
不减仓。事后 30 天平均回撤 -18%（最大 -27%）。

**纪律建议**：当 NDQ.AX 集中度突破 70% 阈值且 RSI > 65 时，
强烈建议减仓 15-20% 释放子弹。
```

Risk Officer 在 Round 1 prompt 看到这条 → Round 2 cross-challenge 时大概率在反驳 Quant 看涨信号 → CIO 综合后更倾向 TRIM。

**这就是"系统在学习"的具体机制**——不是 LLM 自己悟，是统计 + 阈值门固化。

---

## 5. 当前状态（2026-05）

`jobs/dreaming.yml`：

```yaml
name: dreaming
schedule: "0 3 * * *"   # 每天凌晨 3 点
enabled: false          # 默认 disabled
```

**为什么默认 disabled**：
- Light Sleep 一次 LLM 调用 ~¥0.5（90 天数据要分批）
- 还在调阈值门，过早开会污染 insights
- 单人测试还在收集 baseline

**怎么手动跑一次试**：
```bash
uv run python -m jobs.dreaming
```

跑完看 `.dreams/events.jsonl` 三阶段全审计；通过门的会出现在 `memory/insights/`。

---

## 6. GUI 透视

`/system` → "Dreams" tab 实时看：
- short-term-recall.json 当前规模
- 最近 events 流（哪些 candidate 通过 / 没通过 + 原因）

`/system` → "长期模式" tab 看 insights/*.md 所有已固化模式。

---

## 7. 失败模式 / 已知坑

| 坑 | 缓解 |
|----|------|
| LLM 在 Light Sleep 编造信号（数据少时）| Light Sleep 强制走机械摄入，不让 LLM 改字段 |
| Deep Sleep 写入并发（cron + 手动同时跑）| `core/consolidation_lock.py` 提供独占锁 |
| insights 文件越积越多 | 计划加"老化机制"：60 天未触发 → archive；目前未实现 |
| LOOKBACK_DAYS=90 在重度交易者那里不够 | env 可调 |

---

## 下一步

→ [02-agents.md#risk-officer](02-agents.md) 看 insights 在 Risk Officer prompt 里的具体位置

→ [05-data-model.md#memory-目录布局](05-data-model.md) 看 `.dreams/` 整体结构

→ 论文级背景：[OpenClaw Dreaming Guide](https://dev.to/czmilo/openclaw-dreaming-guide-2026-background-memory-consolidation-for-ai-agents-585e)
