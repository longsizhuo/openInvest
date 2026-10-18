---
type: wiki-chapter
title: 数据模型
tags: [data-model, portfolio, schema, concurrency]
intent: 数据模型
schema_source:
  - src/openinvest/core/schemas.py:Holding
  - src/openinvest/core/schemas.py:PortfolioData
  - src/openinvest/core/schemas.py:Activity
  - src/openinvest/calc/ledger_replay.py:replay_ledger
  - src/openinvest/core/portfolio_manager.py:PortfolioManager
  - src/openinvest/core/portfolio_manager.py:HoldingsView
documents:
  endpoints: []
  config_keys: []
  symbols: [with_portfolio_tx, _file_lock, _atomic_write_text, replay_ledger]
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
gold_grams: 30.5
gold_avg_cost_cny_per_gram: 750.00
---
```

**问题**：
- 加新资产（如苹果股票 AAPL）要改代码：portfolio.md 加 `aapl_shares` 字段、PortfolioManager 加 getter、render_body 加渲染逻辑、NapCat bot（历史 connector，2026-07-05 已删除）加 `/aapl_set` 命令——**至少 4 处**
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
    units: 30.5
    unit_label: 克
    avg_cost: 750.00
    cost_currency: CNY
    channel: 浙商积存金
    yfinance_proxy: GC=F
    proxy_kind: gold_cny_per_gram
    sell_fee_pct: 0.0038
---
```

可选字段 `synced_trade_ids`（#231，自动维护勿手改）：`PATCH /api/trades/{id}/status`
已入账单子的 `<trade_id>@<ts>` 去重键（最近 500 个），与 cash/holdings 同一次 atomic
write，保证崩溃重试不双记。见 [ADR-016](adr/016-ledger-mutation-idempotency.md)。

**核心抽象**：
- `cash`：`dict[currency_code, amount]`
- `holdings`：`list[holding]`，每个 holding 自描述（symbol / kind / units / unit_label / cost / channel）

**好处**：
- 加新资产 = 一条 CLI / MCP 调用（`uvx openinvest buy` 或对 agent 说 "track X"，无需改代码）
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
| `bond` / `fund` | 同 stock，但展示标识不同 |
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

**黄金（`GC=F`）只能按 人民币/克 记账**：`units` = 克数、`avg_cost` = CNY/克、`cost_currency: CNY`、`unit_label: 克`。
全仓估值（status / what_if / 委员会 / 日报）都把 `GC=F` 当 CNY/克，按 USD/盎司记会和克价对不上（假浮亏 −78%，#443）。
所以 `buy` / `record_existing_position` 对 `GC=F`（大小写不敏感，统一写成 `GC=F`）拒收非 CNY 币种和非克的单位并给出正确写法
（`buy --symbol GC=F --units 5 --price 880 -c CNY --kind metal --unit-label 克`），合法记法自动落 `kind: metal` +
`proxy_kind: gold_cny_per_gram`。拒绝而不自动换算：换算要猜汇率，且 USD 记账扣的是 USD 现金。旧版本留下的 USD/oz 黄金行会让定投每天报错，`doctor` 为此出一条非阻塞的 `gold_booking` warning（不影响总 `status`），修法是 `delete_holding --force` 后按 CNY/克重记。
- 未来扩展：`crypto_cny`（走 USDT 中转）等

### `is_tracking_only`

建仓时标记追踪仓（CLI / MCP `buy` 的追踪语义，或直接在 portfolio.md 设
`is_tracking_only: true`）。
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

`core/memory_store.py:_file_lock`（sidecar `<path>.lock` + flock，**用后即删**）：

```python
@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    while True:
        fp = open(lock_path, "a")
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        if os.fstat(fp.fileno()).st_ino == os.stat(lock_path).st_ino:
            break              # 锁住的确实是路径上的那个 inode
        fp.close()             # 等锁期间被前任 unlink 了 → 重来
    try:
        yield
    finally:
        os.unlink(lock_path)   # 仍持锁时删；close 才释放
        fp.close()
```

inode 核对是为了让"用后删除"不破坏互斥：否则已 open 旧 inode 的等待者与新建同名文件的第三方会各持一把锁（`tests/test_file_lock_and_jsonl.py` 用多线程 + 多进程 RMW 守这一点）。所有 append-only jsonl 也走同一把锁：`memory_store.append_jsonl(path, record)`（`llm_usage.jsonl` / `executions.jsonl` / `portfolio_history.jsonl` / `.dreams/events.jsonl`）。

→ 同一文件多进程（CLI / MCP server / invest-web / scheduler）同时写，**不会丢更新**。

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
20 轮 scheduler 扣款 + 并发存款 race     → delta 精确 = -37880  (0 lost updates)
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
├── .committee/                # 委员会 transcript（每日每标的）+ 异步任务状态
│   ├── <date>/<sym>.md        # 当日最新一次裁决（所有读方的唯一可信源；含 Input Health 块）
│   ├── <date>/<sym>_path.json # 决策时点路径分布快照（path_review 90 天后回看）
│   ├── <date>/runs/<sym>_<HHMMSS>.md  # 同日重跑时被归档的早先 transcript（2026-10 起不再覆盖丢失）
│   └── <task_id>/
│       ├── status.json
│       └── tool_calls.jsonl
├── .runs/                     # （预留）run/session 数据模型
├── .dreams/                   # Dreaming 子系统私有 + 复盘账本
│   ├── short-term-recall.json
│   ├── candidates.json
│   ├── events.jsonl
│   ├── interventions.jsonl    # 规则干预账本（原始 verdict → 最终 verdict，反事实记账）
│   ├── verdict_review.jsonl   # live 决议事后命中率（verdict_review 每日 cron 整份重写，只 live）
│   └── executions.jsonl       # 用户执行/拒绝声明（decision accounting，issue #133 Decision 9）
└── .state/                    # 简单 KV
    └── processed_emails.json
```

详见 [memory_layout.md](../memory_layout.md)。

### 6.1 `portfolio_history.jsonl`：Activity 归一与账户业绩重放

流水记录形状**不统一**（`action` 有 buy / bought / sold / gift / manual_override / dca_reconcile_* …；价格有 `price`
也有 `price_per_unit`+`total_amount`；外部现金流靠 `funding_source=external_funding` 或 `deposit`/`withdraw` 识别）。
**唯一读法**是 `calc/ledger_replay.py:replay_ledger(records, alias=...) -> (List[Activity], LedgerState)`
（ADR-027 D2/D3）：按日期序**有状态**地把每条记录折叠成统一的 `core/schemas.py:Activity`，读方只认它的
`cash_effect` / `units_effect` / `external` / `correction`，不再各抄一份别名表（`tests/test_ledger_replay_invariance.py`
守"没人再自抄别名表"）。老记录照旧能读，无法识别的行保留为 `action="unknown"` 并计数（`LedgerState.unknown`），不丢。
`MemoryStore.read_history()` 只留原始读取——`/api/history` 与 `history` 命令按原样展示原始行，不经归一。

| 老 `action`（记录原样）| 归一后 Activity | `cash_effect` | `units_effect` | `external` |
|---|---|---|---|---|
| `buy` / `bought`（`funding_source` 缺省 = 子弹池现金）| `buy` | −总额 | +units | False |
| `buy` / `bought` 带 `funding_source=external_funding` | `buy` | 0 | +units | True |
| `dca_reconcile_backfill` / `dca_reconcile_reverse` | `buy` / `sell`，`source=dca_reconcile` | 0（对账行必带 external_funding）| ±units | True |
| `gift` | `gift`（自成一类，不并进 buy）| 0 | +units | True |
| `sell` / `sold` | `sell` | `net_amount`（缺省 总额−fee）| −units | False |
| `deposit` / `withdraw` | 同名 | ±金额（取款符号两种写法都归一为负）| 0 | True |
| `manual_override` | `correction` | 0 | 新绝对值 − 回放持仓 | False，`correction=True` |
| `delete_holding` | 同名，`correction=True` | 0 | −回放持仓 | False |
| `dividend`（ADR-027 PR2）| 同名 | `amount − tax_withheld`（税后净额）| 0 | False |
| `interest` / `return_of_capital` | 同名 | +金额 | 0 | False |
| `fee` / `tax` | 同名 | −金额 | 0 | False |
| `split` | 同名，`split_ratio` = 新/旧 | 0 | 拆股增量（units × (ratio−1)）| False |
| 其他 / 没有可用日期 | `unknown` | 0 | 0 | False |

PR2 起新写入的记录是 **v2 记录**（`schema_version: 2`）：写入方（`PortfolioManager.record_*`，同一把 `with_portfolio_tx` 锁）把
`cash_effect` / `units_effect` / `external` 算好写进流水，读方**只认这三个字段**；上表是缺这些字段时的推导口径。
DRIP（`record_dividend --reinvest`）写两条：`dividend`（+净额）+ 一条 `buy`（`source=drip`，`external=False`，−成本、+份额，
`idempotency_key` 为 `<key>#drip`）。写入入口：`record_dividend`（`--kind interest` = 利息）/ `record_fee`（`--kind tax` = 税）/
`record_split` / `record_return_of_capital`——CLI 子命令与 MCP 工具同名，都带 `idempotency_key`（ADR-016）。

数值字段（units / price / total_amount / net_amount / fee）解析不了的行：`action` 分类照旧、`unparsed=True`、效果全 0，
不进回放、计入 `LedgerState.unknown`——一行脏数据不会让账户业绩或决议账本视图整个挂掉（`Activity.replayable` 为 False）。

`LedgerState` 是回放累计态（每 symbol 的 units / 近似 avg_cost + 每币种现金 Σ，从零开始，**不是**当前快照、不写回快照）。
外部现金流金额用 `external_flow(activity)` 取：deposit/withdraw 就是 `cash_effect`；external_funding 的买卖与赠股是
新钱直接变持仓（`cash_effect`=0），流量 = ±总额。dividend / interest / fee / tax / return_of_capital 是**内部流**（进 NAV、
不进 TWR 分母与 XIRR 现金流——它们就是账户的收益 / 成本），split 零现金效应。价格口径写在 `calc/ledger_replay.py` 的 docstring，
摘要见下面"价格口径"。

账户业绩（`status.performance` / `openinvest performance` / 日报"账户业绩"段，`services/account_performance.py`
→ 纯计算 `calc/account_metrics.py`）把 Activity 重放成日度 NAV 序列，再算 TWR / XIRR / 最大回撤 / 波动 / vs 基准：

| Activity | 持仓 | 现金 | 外部现金流（进 TWR 分母、XIRR） |
|---|---|---|---|
| `buy`（内部）| +units | −成本 | 无（内部转换）|
| `buy` / `sell` 带 `external`、`gift` | ±units | 不动 | ±成本（外部新钱）|
| `sell` | −units | +`cash_effect` | 无 |
| `deposit` / `withdraw` | — | ±金额 | ±金额 |
| `correction` / `delete_holding` | 置为绝对值 | 不动 | 增量 × 当日收盘（持仓修正不是收益）|
| `dividend` / `interest` / `return_of_capital` | 不变 | +金额 | 无（内部流：收益）|
| `fee` / `tax` | 不变 | −金额 | 无（内部流：成本）|
| `split` | 数量换算成拆股后口径（见下）| 不动 | 无 |

- **锚定当前状态**：流水早于系统上线的持仓/现金没有记录，所以开盘状态 = `portfolio.md` 当前值 − Σ流水增量，
  序列末日 NAV 恒等于 status 总资产；差额列在 `data_quality.unreconciled`（补录请用 `buy --existing-position`）。
- 黄金流水符号 `GOLD-CNY` 映射到 `proxy_kind=gold_cny_per_gram` 的持仓，克价 = `GC=F`/31.1035 × `USDCNY=X`。
- **价格口径（`price_basis`，ADR-027 PR2）**：`daily_prices.close` 是 yfinance 复权价（Adj Close：拆股 + 分红都复权；regime /
  指标 / forward_return 读它，语义不变）；同表新增 `close_raw` = yfinance `Close`（只按拆股复权、不含分红复权）。账本里的分红
  是真钱，历史市值再用复权价就把同一笔分红算两次——所以估值时某 symbol 在所需日期范围内（成立日前最后一根 bar 起）
  `close_raw` 每天都有 → 用原始价（`price_basis[sym]="raw"`），缺一天 → **整只**回退复权价（`"adjusted"`），绝不混用。
  回填历史用 `openinvest backfill_raw_close [--symbols A,B]`（默认持仓 + target_assets；场外基金只有前复权净值、黄金克价代理
  是期货 × 外汇无分红，二者不回填，恒为 `adjusted`）；新抓取（`get_history_data`、`scripts/backfill_history`）已两列一起写。
  复权价回退的标的，已记的分红 / 退回本金 / DRIP 再买入**不进回放**（价格里已含，不双计），`data_quality.income_in_adjusted_price`
  列出条数，补齐 close_raw 后自动改按原始价 + 账本现金计入。
- **拆股**：两列价格都已是拆股后口径，所以回放数量必须全程拆股后口径——拆股前的买卖 / 修正数量 × 此后同标的所有 `split` 的
  `split_ratio` 之积（`calc.ledger_replay.later_split_factors`）；split 本身在这个口径下是空操作，NAV 不会在除权日跳变。
- `status.income_30d`：最近 30 天分红（税后）/ 利息合计（按币种；没有记录就不出这个键）。
- 估值只读行情库 `db/market_data.db`（不联网），复用 `utils.fx.total_portfolio_value(rate_override=当日汇率)`；
  网格 = 持仓标的交易日并集，缺价日前值填充并计入 `data_quality.filled_days`。
- 公式：TWR 日度链接 `r_d = (End_d + Out_d − Start_d − In_d) / (Start_d + In_d)`；XIRR 二分法、年 = 365.25 天、
  历史 < 30 天不报；回撤 / 波动（`stdev(ln(1+r)) × √252`）在链接指数上算；基准来自 `portfolio.benchmark_symbol`。

### 6.2 日报"今日持仓归因"怎么算

日报置顶一段（TL;DR 之后、宏观之前；`services/holdings_digest.py` → 纯计算 `calc/attribution.py`；email 是表格、
chat 是逐行列表，两个变体同一份数字）：今天每个持仓涨跌多少、谁拖累了组合、和基准比如何、近期有什么事。

- **口径**：每个持仓用**自己**最近两根收盘（休市 / 不同交易所日历不同不强行对齐，和多数不同日期的持仓在表里标自己的日期；
  周末 / 长假标"最近收盘是 MM-DD"）。`V = units × 收盘 × 汇率(计价币种→基准币种)`，两个时点的汇率都读行情库。
- **组合 = 持仓合计，不含现金**（拿来和基准比的是投资部分）；含现金的净值口径是上面的"账户业绩"，两者数字不同是正常的。
- **每持仓**：`今日涨跌`（基准币种）= `本币涨跌 + 汇率影响`（汇率影响含价格×汇率交叉项，严格相加）；
  `对组合贡献` = 昨日权重 × 当日收益（单位 pp），各持仓相加 = 组合日收益；`对比基准` = 持仓涨跌 − 基准同期涨跌（pp）。
  基准是 `portfolio.benchmark_symbol` 自己最近两根收盘的价格涨跌（不做汇率换算）。
- **降级**：缺价 / 缺汇率 / 少于 2 根 / 最新收盘比今天旧 10 天以上 → 该持仓"未计入归因"并写原因，不进权重分母；
  基准取不到 → 不比较；事件库 / 日历读取失败 → 对应行写 `n/a`，不影响归因，更不阻断日报。
- **近 7 天相关事件**：`db/events.db` 里 symbols 命中持仓（含 ETF→指数代理，同召回口径）、severity≥mid、入库 7 天内，
  每持仓最多 2 条（severity 降序、相似转述去重），标题 + 来源。这是**已发生**的新闻，不是前瞻日历
  （入库时 ts 晚于入库时刻的事件会被改写成入库时刻）。
- **未来 7 天日历**：只查个股，yfinance `Ticker.calendar` 的财报 / 除息 / 派息日，落在 [今天, 今天+7] 的才列；
  取不到（空 / 异常 / 15 秒墙钟超时，工作线程是守护线程、不拖进程退出）写"无数据源"。联网只有这一步和基准行情刷新
  （经 `account_performance._benchmark_closes`，每进程每天最多一次，"账户业绩"段通常已刷过）；持仓行情、汇率、事件都只读本地库。

---

## 7. 历史压测 + 硬化日志

| 死法 | 修法 | 出处 |
|------|------|------|
| 进程被 kill 时 portfolio.md 写到一半，状态损坏 | atomic write 三步 | `core/memory_store.py:_atomic_write_text` |
| 并发存款 + scheduler 扣款 TOCTOU（NapCat bot 时代踩的坑，connector 已删、防御仍在）| 单锁 RMW + transaction | `core/memory_store.py:transaction` |
| schema 飘字段（user 改了 portfolio.md 写了非法字段）| Pydantic v2 强校验 + render_body 用模板 | `core/schemas.py` |
| 多 connector 实现飘移（历史 NapCat bot 自己改 dict）| 强制走 PortfolioManager 接口 | `core/portfolio_manager.py` |

---

## 下一步

→ [02-agents.md](02-agents.md) 看 holdings 数据怎么进入 Risk Officer prompt

→ [07-extending.md#加新资产](07-extending.md#加新资产) 看怎么用 CLI / MCP 新增资产 / 程序化新增

→ [adr/003-v2-data-model.md](adr/003-v2-data-model.md) 看 v1 → v2 决策细节
