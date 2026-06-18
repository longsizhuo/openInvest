---
type: adr
title: "ADR-003: portfolio.md 升 v2 通用 schema"
tags: [data-model, portfolio, schema, migration]
intent: 数据模型
schema_source:
  - core/schemas.py:PortfolioData
  - core/schemas.py:Holding
documents:
  endpoints: []
  config_keys: []
  symbols: []
status: accepted
date: "2026-05-06"
supersedes: []
superseded_by: []
---

# ADR-003: portfolio.md 升 v2 通用 schema

- **状态**: ✅ 已采纳
- **日期**: 2026-05-06
- **决策者**: longsizhuo + Claude

[← ADR 索引](../README.md#架构决策记录-adr) · [完整数据模型文档](../05-data-model.md)

---

## Context

v0.1 portfolio.md 用扁平字段：

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
- 加新资产（如苹果 AAPL）需要在 4-5 处改代码：portfolio.md 字段 / PortfolioManager getter / render_body 模板 / NapCat 命令 / Web API endpoint
- 加新币种（如 USD/EUR）同理
- 字段越来越多，schema 维护成本指数级
- 用户想"追踪一只股看看"必须改代码再部署

观察到 yfinance 覆盖股票 / ETF / 公募基金 / 加密货币 / 商品期货 / 汇率 → **应该让任意 yfinance symbol 都能加**。

---

## Decision

升级到 v2 schema：

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
---
```

**核心抽象**：
- `cash`：`dict[currency_code, amount]`
- `holdings`：`list[holding]`，每个自描述（symbol / kind / units / unit_label / cost / channel / proxy）

---

## Consequences

### 正面

1. **加新资产 0 代码改动**：用户在 GUI 「新增资产」 → 输入 symbol → yfinance Search → 选 → 提交。后端 `/api/holdings` POST 自动追加 `holdings` list。
2. **加新币种 0 代码改动**：`cash["USD"] = 100` 直接生效
3. **支持任意 yfinance ticker**：股票（AAPL）/ ETF（QQQ）/ 港股（0700.HK）/ A 股（005827.SS）/ 加密（BTC-USD）/ FX（USDCNY=X）
4. **proxy_kind 统一抽象代理行情**：浙商积存金（黄金 + USD/CNY 反推克价）也能 fit 进同一个 schema
5. **追踪仓 (`is_tracking_only`)**：用户"我想看看这只股"无需假装持有
6. **代码量减少**：删除了 5 个专用 getter（`gold_grams_held` / `ndq_shares` 等）

### 负面

1. **数据嵌套加深一层**：`p["holdings"][i]["units"]` 而不是 `p["ndq_shares"]`
2. **写代码变啰嗦**：每次找 holding 要 `next((h for h in holdings if h.get("symbol") == sym), None)`
3. **Pydantic schema 复杂度增加**：HoldingV2 字段 12+ vs 旧的扁平字段 5 个
4. **NapCat 11 命令需改写**：`/balance` 从 `p["cash_cny"]` 改 `pm.cash_amount("CNY")`，6 个写命令全要改
5. **要写 v1 read-time fallback**：避免老 portfolio.md 立刻挂

---

## Alternatives Considered

### A. 保持 v1 扁平，加新资产时加新字段

```yaml
cash_cny: 50000
aud_cash: 1000
usd_cash: 0
ndq_shares: 50
aapl_shares: 0
btc_units: 0
...
```

**拒绝**：每加一个资产 5 处改动 + portfolio.md 字段越来越长 + 没法动态追踪任意 symbol。

### B. v2 但用 SQLite

把 portfolio 落 SQLite 表 `holdings(symbol, kind, units, ...)`。

**拒绝**：
- 失去 frontmatter Markdown "LLM 直接读 + git diff 友好" 的好处
- 备份变复杂（`.db` 文件不能 cp 安全）
- 并发安全要重新设计（fcntl 文件锁不够）

### C. v2 但 holdings 用 dict 不是 list

```yaml
holdings:
  NDQ.AX:
    kind: etf
    units: 50
  GC=F:
    kind: metal
    units: 124
```

**拒绝**：YAML 里 dict 顺序不稳定（不同库可能 reorder）；list 自带顺序，UI 显示时一致。

### D. 给 v0.x 用户硬迁移（不做 fallback）

启动时强制跑迁移脚本，旧 portfolio.md 不向下兼容。

**拒绝**：
- 初次迁移失败可能丢数据
- 用户不知道 schema 升级，进系统看到错误体验差

→ 决定加 read-time fallback，写入时自动清旧字段（懒迁移）。

---

## 实施细节

### Read-time fallback

`PortfolioManager.cash` 和 `holdings` 都是 property，读时检测 v1 字段，自动构造等价的 v2 视图：

```python
@property
def cash(self) -> Dict[str, float]:
    cash = dict(self.portfolio.get("cash") or {})
    if not cash:
        cny = self.portfolio.get("cash_cny")
        aud = self.portfolio.get("aud_cash")
        if cny is not None: cash["CNY"] = float(cny)
        if aud is not None: cash["AUD"] = float(aud)
    return cash
```

→ v1 portfolio.md **第一次被读时**自动表现得像 v2，业务代码无感知。

### Write-time 自动清理

`with_portfolio_tx()` 退出时清理 v1 旧字段 + 标记 `schema_version: 2`：

```python
with self.store.transaction("portfolio") as p:
    _ensure_v2_inplace(p)  # 进 with 前自动 fallback
    yield p
    # commit 前：清理 v1 旧字段
    for k in ("cash_cny", "aud_cash", "ndq_shares",
              "ndq_avg_cost_aud_per_share",
              "gold_grams", "gold_avg_cost_cny_per_gram"):
        p.metadata.pop(k, None)
    p["schema_version"] = 2
    validate_portfolio(dict(p.metadata))
    p.set_body(_render_portfolio_body_v2(p))
```

→ v1 portfolio.md **第一次被写**时自动升级到 v2。零停机。

### NapCat 11 命令改写

`connectors/napcat_bot.py` 全部命令切：

| 旧 v1 | 新 v2 |
|------|------|
| `p["cash_cny"] += amount` | `cash["CNY"] = cash.get("CNY", 0) + amount` |
| `p["gold_grams"] = grams` | upsert holding `GC=F` 的 units |
| `p.get("aud_cash", 0)` | `pm.cash_amount("AUD")` |

加了 18 个 fixture 测试覆盖 happy / 余额不足回滚 / multi-currency / RMW 一致性。

### Pydantic schema

`core/schemas.py:PortfolioData` v2 强 schema：
- cash 必须 `dict[str, NonNegativeFloat]`
- holdings 必须 `list[HoldingItem]`
- HoldingItem 必填字段：symbol / kind / units / unit_label / avg_cost / cost_currency / channel

---

## Reference

- 完整数据模型：[05-data-model.md](../05-data-model.md)
- v2 commit: `3353d65 feat: v2 通用化数据模型 + v3 透明化 + live 多轮真辩论 + 18 README`
- NapCat v2 切换 + 测试: `4cc1db3 feat(napcat): 11 命令切 v2 数据模型 + 18 fixture 测试`
- 迁移脚本: `scripts/migrate_portfolio_to_holdings.py`

---

## 复盘触发条件

1. Pydantic schema 复杂度难以维护（HoldingV2 字段 > 25 个）
2. 性能瓶颈出现（read-time fallback 频繁触发，但单人 GB 级数据极不可能）
3. 持仓 100+ 让 list 查找变慢（应改 dict-by-symbol 索引但保留 list 序列化）
4. v3 schema 出现（如加 lots / tax_lots / cost_basis_method）
