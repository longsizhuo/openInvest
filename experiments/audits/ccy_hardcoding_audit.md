# 货币硬编码审计 2026-05-19

审计范围: production code (排除 tests/, experiments/, .venv/)

## 统计

| 类别 | 数量 | 严重度 |
|------|------|--------|
| A. Bug（违反任意币种设计） | 7 | 🔴 |
| B. CNY-base 合理（设计意图） | ~12 | 🟢 |
| C. Display 不致命（货币符号映射） | 3 | 🟡 |
| D. Tests | (略，保持原状) | 🟢 |

---

## A 类 Bug（优先修）

### A1. `core/portfolio_manager.py:200-204` — get_user_status 总市值只支持 CNY/AUD
**代码**:
```python
if ccy == "CNY":
    portfolio_value += value_local
elif ccy == "AUD":
    portfolio_value += value_local * exchange_rate
# 其他币种（USD/HKD 等）暂不折算 —— v3 加 multi-FX 时一起处理
```
**问题**: 注释自承"USD/HKD 暂不折算"。fork 用户持 AAPL(USD)/0700.HK(HKD) 总市值会**漏算**整个非 AUD 部分 → Risk Officer / `UserStatus.portfolio_value` 全线偏低。
**修法**: 改用 `utils.fx.to_base(ccy, value_local, "CNY")`；同时把签名里 `exchange_rate: float` 改成 `as_of_date: Optional[str]=None` 或直接删（让内部走 utils.fx）。

### A2. `core/portfolio_manager.py:153-184` — exchange_rate 参数本身就是单一币种假设
**代码**:
```python
def get_user_status(self, current_prices, exchange_rate: float, ...):
    portfolio_value = cash_cny + cash_aud * exchange_rate
```
**问题**: 把 AUDCNY 当成"the 汇率"传进来；持有 USD/EUR 现金的 fork 用户的 cash 没被折算。
**修法**: 删 `exchange_rate` 参数，函数内部循环 `pm.cash.items()` + `to_base(ccy, amt, "CNY")` 自取所有汇率。

### A3. `jobs/daily_report.py:295,310-314` — 总资产计算只算 CNY+AUD
**代码**:
```python
total_assets_cny = user_status.cash_cny + user_status.cash_aud * current_rate
for h in pm.holdings:
    ...
    if ccy == "CNY":
        total_assets_cny += units * cur
    elif ccy == "AUD":
        total_assets_cny += units * cur * current_rate
    # USD/EUR 等暂不折算（已知缺口，v3 将引入 utils/fx 模块）
```
**问题**: 注释已经标"已知缺口"。`utils/fx` 模块已存在 (`utils/fx.py:to_base`)，但这里没调。
**修法**: 整段循环替换为 `to_base(ccy, units*cur, "CNY")`；现金部分用 `cash_total_in_base(pm.cash, "CNY")`。

### A4. `jobs/daily_report.py:187-208` — AUDCNY 兜底硬编码 4.7
**代码**:
```python
print("⚠️ AUDCNY=X 完全失败，使用历史均值 4.7 兜底")
current_rate = 4.7
data_warnings.append(
    "...AUDCNY 汇率今日无法获取，使用历史均值 4.7 兜底..."
)
...
current_prices["AUDCNY=X"] = current_rate
```
**问题**: 整个 daily_report 假设 AUDCNY 是唯一关心的汇率。fork 用户（持 EUR/USD/JPY）这条 fallback 路径根本拉不到他们要的汇率，且 4.7 是 AUDCNY 历史均值，对其他币种是垃圾值。
**修法**: 改成"按 holdings 的 cost_currency 集合循环拉 X/CNY 汇率"。fallback 不写死数字，缺汇率的资产单独标 stale 不参与总资产。

### A5. `scripts/skill.py:267-274` (cmd_what_if) — _value_in_cny 只支持 CNY/AUD
**代码**:
```python
def _value_in_cny(holding, price: float, fx: float) -> float:
    units = float(holding.get("units", 0) or 0)
    ccy = str(holding.get("cost_currency", "CNY"))
    if ccy == "CNY":
        return units * price
    if ccy == "AUD":
        return units * price * fx
    return units * price  # 其他币种暂当 1:1（v3 多币种再处理）
```
**问题**: 注释承认"其他币种当 1:1"。USD 持仓的 what_if 情景模拟会少乘 ~7 倍。
**修法**: 走 `utils.fx.to_base(ccy, units*price, "CNY")`；情景内可允许 caller 同时改 `audcny / usdcny` 多个 FX。

### A6. `scripts/skill.py:115-119` (cmd_status) — total_assets_cny 写死 NDQ+gold 公式
**代码**:
```python
"total_assets_cny": round(
    cash_cny + aud_cash * audcny
    + ndq_shares * ndq_price * audcny
    + gold_grams * gold_now, 2),
```
**问题**: fork 用户持有 AAPL/0700.HK/BTC-USD 不进总资产。漏掉所有非 NDQ/gold 持仓。
**修法**: 改成 `cash_total_in_base(pm.cash)` + 遍历 `pm.holdings` 走 `to_base` 折算。

### A7. `scripts/skill.py:544-550` (cmd_prepare_committee) — 同 A6
**代码**:
```python
audcny = _safe_close("AUDCNY=X")
gold_now = snap.spot_cny_per_gram if snap else 0.0
total_cny = (
    cash_cny + aud_cash * audcny
    + ndq_shares * _safe_close("NDQ.AX") * audcny
    + gold_grams * gold_now
)
```
**问题**: cmd_prepare_committee（Skill 路径 portfolio_summary）只算 NDQ+gold。Risk Officer 看到的总资产对 fork 用户严重低估。
**修法**: 直接复用 `jobs.daily_report_builder.portfolio_summary_text()` —— 那里 2026-05-19 已经修过、用 `utils.fx.to_base`。删本地手搓 portfolio_summary。

---

## B 类 CNY-base 合理（设计意图，无需修）

- `core/committee.py:340,361,374` — `portfolio_cash_cny` 作为 wealth_context 输入: CNY-base 设计的合理表达。
- `core/committee.py:705` — `Suggested allocation CNY`: 委员会 verdict 单位锚定 CNY（fork 用户也以 CNY 算 alloc）。
- `connectors/web_api.py:271-273,905-906,944-945,1768-1769,3015-3016` — `WriteResponse(cash_cny=..., aud_cash=...)`: v1 API schema 兼容前端用，前端已经长这样。可在 v3 schema 升级时一并改。
- `connectors/napcat_bot.py:129-145` — `/balance`：先打 CNY + AUD，再循环 `pm.cash` 列其他币种。已经通用化，OK。
- `connectors/napcat_bot.py:141-142` — `if ccy in ("CNY", "AUD"): continue` 用来跳过已打印的两个 → display 合理。
- `core/committee.py:668-669` + `capabilities/tools.py:163-164` + `connectors/web_api.py:2481-2486` + `scripts/skill.py:382` — VIX/TNX/USDCNY/AUDCNY macro 面板硬编码：是"通用宏观背景，所有 fork 用户都关心" (web_api 注释明说)。可加 env 让 fork 用户追加更多 FX，但 4 个 baseline 保留是合理 default。
- `core/paper_trade_simulator.py:65-78,82-83` — `ASSET_CURRENCY` 静态映射: 已经手写常见美/澳/港/中股，未知 default USD。Backtest 工具，可接受。
- `services/commsec_reader.py:175` — `"currency": "AUD"`: CommSec 是澳洲券商，写死 AUD 正确。
- `core/portfolio_manager.py:286` — `currency = ... or "AUD"`: 默认 AUD 是历史选择（CommSec only path）。改成 `"USD"` 或要求显式传都行，但当前 caller 都显式传 currency 所以不致命。
- `utils/exchange_fee.py:355-411,420-445` — `get_cost_snapshot` / `format_cost_report` 整段是 CNY→AUD→NDQ 换汇摩擦计算器: 该模块本身就是为 AUD 子弹 + CommSec 用户写的，独立性强；fork 用户用 USD 时整段不调用（daily_report 里有 `has_non_cny` 守门）。可以保留但记 follow-up。

## C 类 Display 不致命（货币符号映射）

### C1. `jobs/daily_report_builder.py:159`
```python
ccy_symbol = "¥" if ccy == "CNY" else ("$" if ccy in ("USD", "AUD") else "")
```
**问题**: EUR/JPY/HKD 持仓的 portfolio_summary 货币符号是空字符串，输出 `"均价 1234.5"` 没单位提示。不致命，但 LLM 提示词清晰度下降。
**修法**: 加 `"HK$" if ccy == "HKD"`, `"€" if ccy == "EUR"`, 兜底直接用 `ccy + " "` (`"EUR 1234.5"`)。

### C2. `connectors/napcat_bot.py:179`
```python
unit_sign = "$" if ccy in ("USD", "AUD", "HKD") else "¥"
```
**问题**: HKD 印 `$` 不致命但应为 `HK$`；EUR 印 `¥` 是错的。
**修法**: 同 C1。

### C3. `jobs/daily_report.py:191,195` — 4.7 字面值出现在 user-facing 提示
"使用历史均值 4.7 兜底" 文案被 inline 拼到 LLM warning。即使 A4 把数字修了，这条 warning 也得动态化。

---

## 推荐方案

### `utils/fx.py` 已有的 helper

```python
def get_fx_rate(quote: str, base: str = "CNY", as_of_date: Optional[str] = None) -> Optional[float]
def to_base(quote: str, amount: float, base: str = "CNY") -> Optional[float]
def cash_total_in_base(cash: dict[str, float], base: str = "CNY") -> tuple[float, dict[str, Optional[float]]]
```

**结论**: 不需要新 helper，三个函数全在了。问题是 7 处 A 类 bug 都**绕过了** `utils.fx`。

### 建议追加 1 个便利函数

```python
# utils/fx.py
def total_portfolio_value_cny(
    pm: PortfolioManager,
    current_prices: dict[str, float],
    base: str = "CNY",
    *, as_of_date: Optional[str] = None,
) -> tuple[float, dict[str, str]]:
    """通用化算总资产: 所有 cash 折算 + 所有 holdings 用 current_prices[sym] × FX 折算
    返回 (total_in_base, {symbol_or_ccy: "ok"/"missing_price"/"missing_fx"})
    """
```

让 A1/A3/A5/A6/A7 五处直接调一行就完事，再没有人手搓 `+ cash_aud * audcny + ndq * fx`。

### 替换映射表

| A 类 bug | 替换为 |
|---------|--------|
| A1 portfolio_manager.get_user_status | 内部循环 `pm.cash` + `holdings` 走 `to_base` |
| A2 exchange_rate 参数 | 删该参数，标 deprecated 一个 release |
| A3 daily_report.py:295,310-314 | `total_portfolio_value_cny(pm, current_prices)` |
| A4 daily_report.py:191 fallback 4.7 | 改"逐币种取汇率"，缺则标 stale |
| A5 skill.py:_value_in_cny | `to_base(ccy, units*price, "CNY")` |
| A6 skill.py cmd_status total_assets_cny | `total_portfolio_value_cny(...)` |
| A7 skill.py cmd_prepare_committee | 复用 `daily_report_builder.portfolio_summary_text` |

### 顺手清理

- `utils/exchange_fee.py:get_cost_snapshot` AUDCNY=X 硬编码 + 文案 `Scenario 1/2/3`: 重命名 `get_aud_cost_snapshot()` 让模块名标识 AUD 专用；fork 用户用别的子弹时直接绕过此模块。
- `connectors/web_api.py` 的 `CashSummary` / `WriteResponse.aud_cash` 是 v1 schema，留 v3 一起改不阻塞。
- `pnl_snapshot.py:130,150-151` 全 v1 legacy（已经写死 NDQ+gold），整文件值得 v3 通用化 follow-up。
