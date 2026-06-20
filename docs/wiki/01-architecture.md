---
type: wiki-chapter
title: 架构总览
tags: [architecture, pipeline, committee, connectors, concurrency]
intent: 系统分层心智模型
documents:
  endpoints:
    - POST /api/committee/run
    - GET /api/committee/live/{task_id}
  config_keys: []
  symbols:
    - run_committee
    - PortfolioManager
    - with_portfolio_tx
---

# 架构总览

> 给开发者建立心智模型的第一篇。读完应能回答："数据从哪来 → 经过几手 → 决策怎么形成 → 写到哪去"。

[← 回 Wiki 索引](README.md) · [下一章：4 角色委员会 →](02-agents.md)

---

## 一句话

**openInvest 是一个 4 层流水线**：外部触发器 → LLM 决策核 → 业务态机 → 落盘。
每层都可独立替换，互不知道对方实现细节。

```
┌─────────────────────────────────────────────────────────────┐
│  CONNECTORS（外部触发器，多消费者模式）                       │
│  napcat_bot.py · web_api.py · skills/invest/scripts/run.sh  │
└────────────────┬────────────────────────────────────────────┘
                 │  调用业务函数（不直接接 LLM）
                 ▼
┌─────────────────────────────────────────────────────────────┐
│  AGENTS（LLM 角色定义 + 提示词）                              │
│  macro · quant · risk · cio  ← 都受 core.committee 编排      │
└────────────────┬────────────────────────────────────────────┘
                 │  Coordinator-Worker 编排，信息分隔
                 ▼
┌─────────────────────────────────────────────────────────────┐
│  CORE（业务核心 + 并发安全 + Pydantic 校验）                  │
│  committee · portfolio_manager · memory_store · regime      │
└────────────────┬────────────────────────────────────────────┘
                 │  fcntl 锁 + atomic write
                 ▼
┌─────────────────────────────────────────────────────────────┐
│  MEMORY（frontmatter Markdown 持久化）                        │
│  portfolio.md · strategy.md · daily/<date>/<sym>.md         │
└─────────────────────────────────────────────────────────────┘
```

---

## 1. Connectors 层（多消费者）

外部世界进入 invest 的入口。当前 3 个 connector，**功能等价、各自负责自己的协议**：

| Connector | 触发协议 | 适合 |
|-----------|---------|------|
| `connectors/napcat_bot.py` | QQ WebSocket 私聊命令（`/balance` `/deposit 100`）| 移动端快速操作 |
| `connectors/web_api.py` | HTTP REST + SSE | Web GUI / 程序化集成 |
| `skills/invest/scripts/run.sh` (CLI) | Claude Code Skill | 让 Claude 自己当协调者跑 |

**关键约束**：connector 必须**只做协议转换**，业务逻辑全部 forward 给 `core/`。
违反这条 → connector 之间会出现行为飘移（已有教训：早期 napcat 自己改 portfolio dict 导致 Web 写入失败）。

详见各 connector 子目录 README：
- [connectors/README.md](../../connectors/README.md)
- [skills/README.md](../../skills/README.md)

---

## 2. Agents 层（LLM 角色）

四个独立 LLM session，**信息隔离**：

| Role | 看得到 | 看不到 | 输出 |
|------|--------|--------|------|
| Macro Strategist | VIX / TNX / USDCNY / 全球宏观 | 用户持仓、技术指标 | SIGNAL + STRENGTH + SCORE |
| Quant Analyst | 技术指标（RSI/MA/分位）+ REGIME 概率口径 + 估值/情绪事实块 | 用户持仓 | 看涨/看跌 + 信号强度 |
| Risk Officer | 持仓集中度 / 浮盈缓冲 / Dreaming insights | 技术指标 | 风险等级 + 集中度评分 |
| CIO | 三人完整 transcript | — | BUY/ACCUMULATE/HOLD/TRIM/SELL + confidence |

**Cross-Challenge 多轮辩论**（v3）：
- Round 1：Quant + Risk 独立陈述（并行）
- Round 2..N：互看对方上轮输出，调整自己（最多 4 轮）
- 收敛检测：SIGNAL + STRENGTH 两轮稳定 → 提前退出
- CIO 在 transcript 完整后综合出 verdict

详见 [02-agents.md](02-agents.md)。

---

## 3. Core 层（业务核心）

### 3.1 编排器：`core/committee/` 包（`debate.py:run_committee` 为入口）

```python
def run_committee(asset, market_data, macro_view, portfolio_summary,
                  *, max_debate_rounds=4, progress_callback=None):
    # Round 1: Quant + Risk parallel (信息隔离)
    # Round 2..N: cross-challenge with 收敛检测
    # CIO: 综合 transcript 出 verdict
```

`progress_callback` 让 Web SSE 端点可以实时 push stage 进度（"round_1_done" / "round_2_done" / "cio_done"）。

### 3.2 状态机：`core/portfolio_manager.py`

```python
with pm.with_portfolio_tx() as p:
    cash = dict(p.get("cash") or {})
    cash["CNY"] = cash.get("CNY", 0) + amount
    p["cash"] = cash
# with 退出时自动: validate → render body → atomic write
```

**所有写操作必须走这个 RMW 闭包**——保证 fcntl 文件锁 + Pydantic schema validate + 原子写。
异常时整个 tx 不落盘（commit-on-success）。

### 3.3 数据校验：`core/schemas.py`

Pydantic v2 强 schema，写入前必过：
- `cash` 必须是 `dict[str, NonNegativeFloat]`
- `holdings[].units >= 0`
- `holdings[].symbol` 必须非空
- 任何不满足 → ValidationError，写不进 memory

### 3.4 REGIME 分类与概率口径：`core/regime.py` + `core/regime_probability.py`

6 类 regime（uptrend/downtrend/range_bound/crash/recovery/unknown），**确定性算法
不走 LLM**。2026-05-31 起 regime 不再翻译成方向锁（消融证明方向锁层=1 个杠杆+2 个
摆设），改为**中性概率口径**喂给 Quant：

```
uptrend / downtrend / range_bound / recovery
            → 该 (asset, regime) 的 OHLC 30d forward return 概率口径
              （中位 / 跌破现价概率 / 样本数），方向判断交回 LLM + 数据
crash       → 强制 neutral（可执行性约束，非方向预测）
unknown     → 维持原计划（缺数据保守默认）
```

确定性的方向干预走 `parse_cio_memo` 后处理：快崩防御（VIX 哨兵 OR ATR 波动突变比
→ 买侧降级）与显式风险档（`risk_profile=aggressive` 的 uptrend 杠杆，默认关）。
概率表还提供 30/60/90 多窗分布 + 四类路径形状（见
[15-path-probability.md](15-path-probability.md)）。

详见 [02-agents.md#regime-分类与中性概率口径](02-agents.md#regime-分类与中性概率口径)。

---

## 4. Memory 层（Markdown 持久化）

不用关系数据库，用 frontmatter Markdown：

```markdown
---
schema_version: 2
cash:
  CNY: 50000
  AUD: 1000
holdings:
  - symbol: NDQ.AX
    units: 50
    avg_cost: 38.50
    cost_currency: AUD
---

# 当前持仓
- CNY 现金: ¥50,000
- NDQ.AX: 50 股 @ A$38.50
```

**为什么这样**：
- frontmatter 给代码读写（atomic）
- body 给 LLM 直接看（不需要二次格式化）
- 同一份文件，Python 和 LLM 看到的永远一致
- git diff 友好，备份只需 cp -a

详见 [05-data-model.md](05-data-model.md) 和 [memory_layout.md](../memory_layout.md)。

---

## 5. 数据流（一次完整委员会）

以 `POST /api/committee/run` 为例：

```
1. web_api.committee_run()
     │
     ├─ 创建 task_id，写 .committee/<task_id>/status.json (queued)
     ├─ asyncio.create_task(_run_committee_task)
     └─ 立即返回 task_id 给前端
2. _run_committee_task (async, 后台跑)
     │
     ├─ 拉 strategy.target_assets → symbols
     ├─ run_macro_view(macro_data) ← 跨资产共享
     ├─ on_progress({phase: "macro_done"})
     │
     ├─ for sym in symbols (ThreadPoolExecutor 并行):
     │     │
     │     └─ run_committee_for_symbol(sym, ...)
     │           │
     │           ├─ get_history_data(sym, "2y")
     │           ├─ compute_metrics(df) → regime
     │           ├─ regime_brief = format_regime_brief(metrics)
     │           │
     │           ├─ Round 1: _parallel_ask([quant, risk])
     │           ├─ on_progress({phase: "round_1_done", round: 1, ...})
     │           │
     │           ├─ Round 2..N: cross_challenge with 收敛检测
     │           ├─ on_progress({phase: "round_N_done", ...})
     │           │
     │           ├─ CIO ask 综合 transcript
     │           ├─ on_progress({phase: "cio_done"})
     │           │
     │           └─ 落盘 daily/<date>/<sym>.md
     │
     └─ 写 .committee/<task_id>/status.json (done + result)

3. 前端订阅 SSE GET /api/committee/live/{task_id}
     │
     └─ 每个 on_progress 写 status.json 后 → SSE 流推 event
```

**关键点**：
- 前后端 1.5s 内拿到 task_id，不阻塞 HTTP
- 后台 ThreadPool 让多资产真并行（之前 v2 是串行）
- 每 stage 写一次 status.json，SSE 端点 watch 文件 mtime
- 25s keepalive 防 CF Access 5min idle 超时

---

## 6. 并发模型

四个并发场景：

| 场景 | 风险 | 缓解 |
|------|------|------|
| napcat 存款 + scheduler 扣款同时跑 | TOCTOU 丢更新 | `MemoryStore.transaction()` 单锁 RMW |
| 多线程 ThreadPool 同时调 LLM | （只读不冲突）| — |
| Web API 写 + napcat 写同时 | TOCTOU | 同上，fcntl 是进程级锁 |
| 多个 committee task 同时跑 | status.json 互踩 | 每 task_id 一个独立 dir |

并发压测（`tests/test_memory_store.py`）：
```
50 线程并发 cash["CNY"] += 1   → 最终 delta = 50.0  (0 lost updates)
20 轮 scheduler 扣款 + napcat 存款 race → delta 精确 = -37880  (0 lost updates)
```

详见 [05-data-model.md#并发安全](05-data-model.md#并发安全)。

---

## 7. 扩展点

每层都设计成可独立替换：

- **加 connector**（如 Telegram bot）：照 napcat_bot.py 模式新建一个文件，**不要碰 core**
- **加 agent 角色**（如 ESG 分析师）：在 `agents/` 加 prompt 文件，在 `core/committee/debate.py:run_committee` 注册
- **换 LLM provider**（DeepSeek → OpenAI）：改 `agents/agent.py` 的 client init，prompt 不动
- **换持久化**（Markdown → SQLite）：实现 `MemoryStore` 同接口，core 不动

详见 [07-extending.md](07-extending.md)。

---

## 下一步

→ [02-agents.md](02-agents.md) 看 4 个 LLM 角色具体在做什么 + 怎么 cross-challenge

→ [05-data-model.md](05-data-model.md) 看 v2 schema 演进 + 并发安全细节

→ [07-extending.md](07-extending.md) 看怎么加新功能
