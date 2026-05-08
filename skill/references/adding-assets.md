# 添加新资产（用户想跟踪 AAPL / TSLA / 005827 等时读）

默认 onboarding 只配两个资产（NDQ.AX + GC=F）。v2 schema 支持任意 yfinance symbol。
三种添加方式，按推荐度排：

## 方式 1：Web GUI（首选）

用户打开 `http://localhost:8765`（或部署后的 `invest.<域名>`）→ Dashboard →
`[+ 新增资产]` 按钮 → 搜索框 → 选 yfinance 命中项 → 填表单 → 提交。

搜索框走 yfinance 免费 Search API（不用额外 key）。覆盖股票、ETF、公募基金、
加密货币、汇率、期货。

**"我只想看不想持有"** 场景：
勾选 `is_tracking_only` 复选框。追踪仓不计入总资产 / PnL，但委员会照样能分析。

## 方式 2：REST API（GUI 没起时）

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

用户得在 Cloudflare Access 鉴权后面打 API（或者本地 `curl http://127.0.0.1:8765/...`
直连服务器）。你够不到他服务器，多半要指他自己跑 `curl`。

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

用户通过 GUI/API 加完后，让他们（或你帮他）跑 `~/.claude/skills/invest/scripts/run.sh status`
看 `all_holdings` 里有没有新 symbol。
