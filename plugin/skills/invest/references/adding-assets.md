# 添加新资产（用户想跟踪 AAPL / TSLA / 005827 等时读）

默认 onboarding 只配两个资产（NDQ.AX + GC=F）。v2 schema 支持任意 yfinance symbol。
三种添加方式，按推荐度排：

## 方式 1：CLI `buy`（首选，用户真的持有时）

```bash
~/.claude/skills/invest/scripts/run.sh buy --symbol AAPL --units 100 --price 150 -c USD --kind stock
```

MCP 用户直接调 `buy` 工具，参数同名。加权平均成本自动算，symbol 自动进追踪。

**"我只想看不想持有"** 场景（issue #179 起有原生入口）：
```bash
~/.claude/skills/invest/scripts/run.sh track_asset --symbol AAPL --max-single-invest-cny 8000
```
MCP 用户直接调 `track_asset` 工具（幂等 upsert：重复 track 不报错，只更新传入
字段）。它把 symbol 加进 strategy 的跟踪列表——委员会/DCA 的覆盖面由它决定；
`untrack_asset` 移除，`set_allocations` 改股票/现金目标配比。
仍要"零仓位挂在持仓表里"的展示需求才走 `POST /api/holdings` 带
`is_tracking_only: true`，或干脆不持久化直接分析（方式 3）。

## 方式 2：REST API（长尾：追踪仓 / remote hub）

```http
POST /api/holdings
Content-Type: application/json

{
  "symbol": "AAPL",
  "kind": "stock",
  "units": 0,
  "unit_label": "股",
  "avg_cost": 0,
  "cost_currency": "USD",
  "channel": "Robinhood",
  "is_tracking_only": true
}
```

`kind` 枚举：`stock` / `etf` / `metal` / `crypto` / `bond` / `fund` / `other`。

Web API 已 deprecated（只服务 remote hub 模式）——能用 CLI `buy` 覆盖的场景
优先 CLI，只有 `is_tracking_only` 这类 CLI 没暴露的字段才 curl。

## 方式 3：用户只想分析不想持久化

如果用户说 **"该不该买 TSLA"** 但还不想加 TSLA 进 portfolio，可以直接分析：

```bash
~/.claude/skills/invest/scripts/run.sh prepare_committee TSLA
```

委员会能分析任意 yfinance symbol，不管是否在 holdings 里。输出会落到
`memory/.committee/<date>/TSLA.md` 留 history，但 TSLA 不进 portfolio。

适用：
- 用户在 brainstorm，没决心 track
- symbol 是一次性的（如响应新闻）
- 用户明说"就给我看法，不用加进去"

## yfinance symbol 格式

底层数据源是 yfinance。常见格式：

| 市场 | 格式 | 示例 |
|------|------|------|
| 美股 | 裸 ticker | `AAPL`、`TSLA` |
| 美股 ETF | 裸 ticker | `SPY`、`QQQ` |
| 澳交所（ASX）| `XXX.AX` | `NDQ.AX`、`BHP.AX` |
| 港交所（HKEX）| `XXXX.HK` | `0700.HK`、`9988.HK` |
| 上交所 | `XXXXXX.SS` | `600519.SS`、`005827.SS`（公募基金）|
| 深交所 | `XXXXXX.SZ` | `000001.SZ` |
| 伦交所（LSE）| `XXX.L` | `BP.L`、`HSBA.L` |
| 东交所（TSE）| `XXXX.T` | `7203.T` |
| 加密 | `XXX-USD` | `BTC-USD`、`ETH-USD` |
| 汇率 | `XXXYYY=X` | `USDCNY=X`、`AUDCNY=X` |
| 商品期货 | `XX=F` | `GC=F`（黄金）、`CL=F`（原油）|

用户说 "AAPL" 直接用就行。说 "茅台" 要先转 `600519.SS` 再传给 API。

## yfinance **不**支持的

老实告诉用户：

- ❌ 银行理财（如招行朝朝盈）
- ❌ 余额宝 / 国债逆回购
- ❌ 私募基金 / 信托
- ❌ 未上市 REITs
- ❌ 小交易所的加密（只支持主流的 `BTC-USD` 类）

这些得加新数据源——见 [docs/wiki/07-extending.md#2-加新数据源](https://github.com/longsizhuo/openInvest/blob/main/docs/wiki/07-extending.md#2-加新数据源)。
你帮不了用户实时加（要改代码），但可以指他们去那篇文档。

## 确认添加成功

加完后，让用户（或你帮他）跑 `~/.claude/skills/invest/scripts/run.sh status`
看 `all_holdings` 里有没有新 symbol。
