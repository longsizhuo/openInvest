---
type: wiki-chapter
title: 数据模型
tags: [data-model, portfolio, schema, concurrency]
intent: 数据模型
schema_source:
  - core/schemas.py:Holding
  - core/schemas.py:PortfolioData
  - core/portfolio_manager.py:PortfolioManager
  - core/portfolio_manager.py:HoldingsView
documents:
  endpoints: []
  config_keys: []
  symbols: [with_portfolio_tx, _file_lock, _atomic_write_text]
---

# 数据模型

> v2 schema（2026-05+）：cash dict + holdings list，任意 yfinance symbol plug-and-play。
> 这一章解释 schema 演进、迁移路径、并发 RMW 闭包。

[← 04-execution-paths](04-execution-paths.md) · [Wiki 索引](README.md) · [06-api →](06-api.md)

---

## 1. 为什么是 Markdown + frontmatter

**抛弃**：
- ❌ 单文件 `user_profile.json`：每次写都要全量加载 + 全量写回 + diff 不友好
- ❌ SQLite：备份要导出，diff 看不了，git ignore 整个 .db 文件粒度太粗

**选**：frontmatter Markdown，一文件一概念：

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
---
# 当前持仓
- CNY 现金: ¥50,000
- NDQ.AX: 50 股 @ A$38.50
```

**好处**：
- frontmatter 给代码读写（atomic write）
- body 给 LLM 直接看（不需要二次格式化）
- 同一份文件，Python 和 LLM 看到的永远一致
- git diff 友好（虽然 memory/ git ignore，但 backup 时人眼可读）
- 备份 = `cp -a memory/ memory.bak/`

---

## 2. v1 → v2 演进

### v1（2026-04 之前，已废）

```yaml
---
cash_cny: 50000
aud_cash: 1000
ndq_shares: 50
ndq_avg_cost_aud_per_share: 38.50
gold_grams: 124
gold_avg_cost_cny_per_gram: 1008.79
---
```

**问题**：
- 加新资产（如苹果股票 AAPL）要改代码：portfolio.md 加 `aapl_shares` 字段、PortfolioManager 加 getter、render_body 加渲染逻辑、NapCat 加 `/aapl_set` 命令——**至少 4 处**
- 加新币种（如 USD/EUR）同理
- 字段越来越多，schema 维护成本指数级

### v2（2026-05+）

```yaml
---
schema_version: 2
cash:
  CNY: 50000
  AUD: 1000
  USD: 0
holdings:
  - symbol: NDQ.AX
    kind: etf
    units: 50
    unit_label: 股
    avg_cost: 38.50
    cost_currency: AUD
    channel: CommSec
    display_name: BetaShares Nasdaq 100 ETF
    proxy_kind: direct
  - symbol: GC=F
    kind: metal
    units: 124.0
    unit_label: 克
    avg_cost: 1008.79
    cost_currency: CNY
    channel: 浙商积存金
    yfinance_proxy: GC=F
    proxy_kind: gold_cny_per_gram
    sell_fee_pct: 0.0038
---
```

**核心抽象**：
- `cash`：`dict[currency_code, amount]`
- `holdings`：`list[holding]`，每个 holding 自描述（symbol / kind / units / unit_label / cost / channel）

**好处**：
- 加新资产 = 用户在 GUI「新增资产」按 1 个按钮（无需改代码）
- 加新币种 = `cash["USD"] = 100`（无需改代码）
- 任何 yfinance symbol（NDQ.AX / AAPL / 005827.SS / BTC-USD）都能存

详见 [adr/003-v2-data-model.md](adr/003-v2-data-model.md) 决策依据。

---

## 3. v1 自动 fallback（无痛迁移）

`PortfolioManager` 在 read-time 自动兜底：

```python
@property
def cash(self) -> Dict[str, float]:
    cash = dict(self.portfolio.get("cash") or {})
    if not cash:
        # v1 fallback：从扁平字段聚合
        cny = self.portfolio.get("cash_cny")
        aud = self.portfolio.get("aud_cash")
        if cny is not None: cash["CNY"] = float(cny)
        if aud is not None: cash["AUD"] = float(aud)
    return cash

@property
def holdings(self) -> HoldingsView:
    raw = list(self.portfolio.get("holdings") or [])
    if not raw:
        # v1 fallback: 从 ndq_shares / gold_grams 构造
        ndq = float(self.portfolio.get("ndq_shares", 0) or 0)
        gold = float(self.portfolio.get("gold_grams", 0) or 0)
        if ndq > 0: raw.append({...})
        if gold > 0: raw.append({...})
    return HoldingsView(raw)
```

**写入时**：`with_portfolio_tx()` 退出时清理所有 v1 字段 + 标记 `schema_version: 2`。

→ **效果**：v1 portfolio.md 第一次被写入时自动升级到 v2。零迁移脚本停机时间。

→ **但建议主动跑迁移**：`python scripts/migrate_portfolio_to_holdings.py` 一次性升级（保留备份）。

---

## 4. Holding 字段详解

```yaml
- symbol: NDQ.AX            # 必填，yfinance 格式 ticker
  kind: etf                 # 必填: stock|etf|metal|crypto|bond|fund|other
  units: 50                 # 必填，持仓数量
  unit_label: 股            # 必填，股 / 克 / oz / 个
  avg_cost: 38.50           # 必填（追踪仓填 0）
  cost_currency: AUD        # 必填，3-5 字母 ISO
  channel: CommSec          # 必填，渠道（CommSec / 浙商积存金 / Robinhood）
  display_name: ...         # 可选，UI 显示用
  proxy_kind: direct        # 可选，行情计算方式
  yfinance_proxy: GC=F      # 可选，行情 ticker 不等于 symbol 时用
  is_tracking_only: false   # 可选，仅观察不计 PnL
  sell_fee_pct: 0.0038      # 可选，卖出费率
```

### `kind` 影响什么

| kind | 影响 |
|------|------|
| `stock` / `etf` | quote 直接拿 yfinance Close 价 |
| `metal` | 走 `yfinance_proxy` + FX 反推（黄金常用 GC=F + USDCNY → CNY/g）|
| `crypto` | 走 BTC-USD / ETH-USD 等 |
| `bond` / `fund` | 同 stock，但 GUI 标识不同 |
| `other` | 兜底类型 |

### `proxy_kind` 是什么

行情代理协议。当 `cost_currency` 和 yfinance 报价币种不一致时用。

例：浙商积存金的均价是 ¥/克，但 yfinance `GC=F` 报的是 USD/oz。
设 `proxy_kind: gold_cny_per_gram` 让 `utils/gold_price.py` 自动：
1. 拉 GC=F → USD/oz
2. 拉 USDCNY=X → 汇率
3. ÷ 31.1035（盎司换克）
4. × 浙商点差（来自 strategy.price_offset_pct）
5. = CNY/克 报价

**proxy_kind 取值**：
- `direct`：symbol 就是 yfinance ticker（NDQ.AX / AAPL）
- `gold_cny_per_gram`：上面那个反推链路
- 未来扩展：`crypto_cny`（走 USDT 中转）等

### `is_tracking_only`

GUI 上勾选"追踪仓"复选框 → `is_tracking_only: true`。
影响：
- `units` 可以为 0
- 不计入总资产 / PnL
- 委员会 Risk Officer prompt 不传它（不影响集中度计算）
- Dashboard 上独立分组显示

→ 用户"我想关注这只股看看"的入口，不必假装持有。

---

## 5. 并发安全

### 5.1 写操作必须走 `with_portfolio_tx()`

```python
from core.portfolio_manager import PortfolioManager

pm = PortfolioManager()
with pm.with_portfolio_tx() as p:
    cash = dict(p.get("cash") or {})
    cash["CNY"] = cash.get("CNY", 0) + 100
    p["cash"] = cash
# with 退出时:
# 1. 清理 v1 旧字段
# 2. p["schema_version"] = 2
# 3. validate_portfolio(p.metadata)  ← Pydantic 强校验
# 4. p.set_body(_render_portfolio_body_v2(p))
# 5. atomic write (tmp → fsync → os.replace)
```

**异常时**：with 块内 raise → 整个 tx 回滚，不写盘（commit-on-success）。

### 5.2 fcntl 文件锁（跨进程）

`core/memory_store.py:_file_lock`：

```python
@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "w") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
```

→ 同一文件 napcat_bot 进程 + invest-web 进程 + scheduler 进程同时写，**不会丢更新**。

### 5.3 atomic write（防进程被 kill）

```python
def _atomic_write_text(path: Path, text: str):
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # POSIX 原子操作
```

→ 进程在写 portfolio.md 时被 kill -9，**最坏情况是新内容没写进，老内容完整保留**。永远不会写一半。

### 5.4 并发压测

`tests/test_memory_store.py`：

```
50 线程并发 cash["CNY"] += 1   → 最终 delta = 50.0   (0 lost updates)
20 轮 scheduler 扣款 + napcat 存款 race  → delta 精确 = -37880  (0 lost updates)
```

---

## 6. memory/ 目录全景

```
memory/
├── MEMORY.md                  # 索引（人类 + agent 都读）
├── DREAMS.md                  # Dreaming 写出的叙事性梦日记（v3 启用）
├── user.md                    # 用户身份 / 风险偏好 / 月薪
├── strategy.md                # 投资策略 / target_assets
├── portfolio.md               # 当前持仓（v2: cash dict + holdings list）
├── portfolio_history.jsonl    # 交易流水（append-only）
├── llm_usage.jsonl            # LLM 调用 telemetry（v3 透明化）
├── insights/                  # Deep Sleep 通过阈值门的长期洞察
│   └── *.md
├── daily/                     # 每日委员会决议（v3 多资产 = 多文件）
│   └── YYYY-MM-DD/<SYMBOL>.md
├── .committee/                # 委员会异步任务状态
│   └── <task_id>/
│       ├── status.json
│       └── tool_calls.jsonl
├── .runs/                     # （预留）run/session 数据模型
├── .dreams/                   # Dreaming 子系统私有
│   ├── short-term-recall.json
│   ├── candidates.json
│   └── events.jsonl
└── .state/                    # 简单 KV
    └── processed_emails.json
```

详见 [memory_layout.md](../memory_layout.md)。

---

## 7. 历史压测 + 硬化日志

| 死法 | 修法 | 出处 |
|------|------|------|
| 进程被 kill 时 portfolio.md 写到一半，状态损坏 | atomic write 三步 | `core/memory_store.py:_atomic_write_text` |
| napcat 存款 + scheduler 扣款 TOCTOU | 单锁 RMW + transaction | `core/memory_store.py:transaction` |
| schema 飘字段（user 改了 portfolio.md 写了非法字段）| Pydantic v2 强校验 + render_body 用模板 | `core/schemas.py` |
| 多 connector 实现飘移（napcat 自己改 dict）| 强制走 PortfolioManager 接口 | `core/portfolio_manager.py` |

---

## 下一步

→ [02-agents.md](02-agents.md) 看 holdings 数据怎么进入 Risk Officer prompt

→ [07-extending.md#加新资产](07-extending.md#加新资产) 看怎么用 GUI 新增资产 / 程序化新增

→ [adr/003-v2-data-model.md](adr/003-v2-data-model.md) 看 v1 → v2 决策细节
