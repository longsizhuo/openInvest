"""Skill 只读视图构建器 — CLI (scripts/skill.py) 与 Web API (connectors/web_api.py) 共享

2026-06 远端模式（hub-and-spoke）重构：status / strategy / history / what_if / doctor
的计算体从 scripts/skill.py 提取到这里，CLI 和 /api/skill/* 端点都调同一份函数，
防止 local / remote 输出形状漂移（漂移历史见 CLAUDE.md 分层契约段）。

约定：
- 每个 builder 返回 dict（不打印、不 sys.exit），错误也以 {"status": "error", ...}
  dict 返回，由调用方决定退出码 / HTTP 状态码
- 外部数据调用（yfinance / gold / fx）保持**函数内延迟 import**——
  tests/test_onboarding_smoke.py 等用 patch("openinvest.utils.exchange_fee.get_history_data")
  的测试依赖 call-time 解析
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from openinvest.core.memory_store import MemoryStore

log = logging.getLogger(__name__)


def _safe_close(symbol: str) -> Optional[float]:
    """最新收盘价；拉不到 / 非有限 → None（不是 0.0 哨兵：live_prices 对外契约是 null）。

    skill_cmds/_helpers.py 重导出本函数（#236：原先逐字复制两份）。
    """
    from openinvest.utils.exchange_fee import get_history_data
    try:
        df = get_history_data(symbol, "1d")
        if df.empty:
            df = get_history_data(symbol, "5d")
        if df.empty:
            return None
        v = float(df["Close"].iloc[-1])
    except Exception as e:  # noqa: BLE001  单个行情源挂了只让这一个字段 null，不拖垮整个视图
        log.warning("_safe_close(%s) 拉取失败: %s", symbol, e)
        return None
    return v if math.isfinite(v) else None


def _is_gold_gram(h: Dict[str, Any]) -> bool:
    """黄金克价口径（CNY/克，get_quote 的 gold_cny_per_gram 反推）：按 proxy_kind 认，不按 symbol 名。

    老行没写 proxy_kind 的 kind=metal 同口径（what_if 也这么认）。
    """
    pk = str(h.get("proxy_kind") or "")
    return pk == "gold_cny_per_gram" or (str(h.get("kind") or "") == "metal" and not pk)


# ---------- status ----------

def build_status_view() -> Dict[str, Any]:
    """v2 通用化：从 cash dict + holdings list 读，所有持仓同一条定价路径（utils.quotes.get_quote）。

    不认 symbol 名：黄金克价口径按 proxy_kind / kind=metal 判断（_is_gold_gram），AUD 等外币段按
    现金/持仓的币种出现与否判断。没持有的东西不拉数、不输出（也不把它的行情回填进行情库）。
    """
    from openinvest.core.portfolio_manager import PortfolioManager
    from openinvest.utils.fx import get_fx_rate, total_portfolio_value_cny, unvalued
    from openinvest.utils.quotes import get_quote
    pm = PortfolioManager()

    cash_cny = pm.cash_amount("CNY")
    held = [h for h in pm.holdings if h.get("symbol") and not h.get("is_tracking_only")]

    # 外币段：现金或持仓用到的非 CNY 币种 → 汇率（拉不到就不出这个键，不编）
    ccys = ({str(c).upper() for c in pm.cash} | {str(h.get("cost_currency") or "").upper() for h in held}) - {"", "CNY"}
    fx_rates = {c: get_fx_rate(c, "CNY") for c in sorted(ccys)}
    audcny = fx_rates.get("AUD") or 0.0

    # 2026-05-19 (A6 修复): total_assets_cny 之前写死 cash + aud*fx + ndq*price*fx +
    # gold*price，fork 用户的 AAPL/0700.HK/BTC-USD 全漏算。改走 utils.fx.total_portfolio_value_cny
    # 通用化遍历所有 holdings + 多币种 cash 折算到 CNY。
    current_prices: Dict[str, float] = {}
    quote_meta: Dict[str, Dict[str, Any]] = {}
    gold_extra: Optional[Dict[str, Any]] = None   # 克价口径持仓的 quote.extra（金价 USD/oz、USDCNY）
    for h in held:
        sym = str(h["symbol"])
        gram = _is_gold_gram(h)
        quote = get_quote({**h, "proxy_kind": "gold_cny_per_gram"} if gram else h)
        if quote is not None and math.isfinite(quote.price) and quote.price > 0:
            current_prices[sym] = quote.price
            quote_meta[sym] = {
                "last_updated": quote.last_updated,
                "is_stale": quote.is_stale,
                "source": (quote.extra or {}).get("source", "yfinance"),
                "extra": quote.extra or {},
            }
            if gram and gold_extra is None:
                gold_extra = quote.extra or {}
    total_cny, _value_status = total_portfolio_value_cny(pm, current_prices, base="CNY")

    # 账户业绩（TWR / XIRR / 回撤 / vs 基准）：账本重放 + 行情库估值，与 openinvest performance 同一函数。
    # 失败不阻断 status（持仓/现金是硬需求，业绩是附加块）。
    try:
        from openinvest.services.account_performance import build_account_performance
        performance = build_account_performance(pm)
    except Exception as e:  # noqa: BLE001
        performance = {"status": "error", "error": f"{type(e).__name__}: {e}"}

    holding_views: List[Dict[str, Any]] = []
    for h in pm.holdings:
        view = {k: h[k] for k in (
            "symbol", "kind", "units", "unit_label", "avg_cost",
            "cost_currency", "channel", "display_name", "is_tracking_only",
            "proxy_kind",
        ) if k in h}
        sym = str(h.get("symbol") or "")
        units = float(h.get("units", 0) or 0)
        avg_cost = float(h.get("avg_cost", 0) or 0)
        price = current_prices.get(sym)
        if price is not None and math.isfinite(price) and price > 0:
            market_value = price * units
            pnl = (price - avg_cost) * units if avg_cost > 0 else None
            ccy = str(h.get("cost_currency") or "CNY").upper()
            rate = 1.0 if ccy == "CNY" else fx_rates.get(ccy)
            view.update({
                "current_price": round(price, 6),
                "market_value": round(market_value, 2),
                # 折人民币市值：外币持仓一视同仁（取代以前只有 NDQ.AX 才有的 ndq.value_cny）；汇率拉不到 → null
                "market_value_cny": round(market_value * rate, 2) if rate else None,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round((price / avg_cost - 1) * 100, 2) if avg_cost > 0 else None,
                **quote_meta.get(sym, {}),
            })
        else:
            view.update({
                "current_price": None,
                "market_value": None,
                "market_value_cny": None,
                "pnl": None,
                "pnl_pct": None,
            })
        holding_views.append(view)

    # 克价口径持仓（黄金）汇总块——兼容老 agent 读 status["gold"]；多个克价持仓合并，逐仓明细看 all_holdings
    gold_hs = [h for h in held if _is_gold_gram(h)]
    gold_block: Dict[str, Any] = {}
    if gold_hs:
        grams = sum(float(h.get("units", 0) or 0) for h in gold_hs)
        gold_avg = sum(float(h.get("units", 0) or 0) * float(h.get("avg_cost", 0) or 0) for h in gold_hs) / grams if grams else 0.0
        gold_now = next((current_prices[str(h["symbol"])] for h in gold_hs if str(h["symbol"]) in current_prices), None)
        gold_block = {
            "grams": grams,
            "avg_cost_cny_per_gram": round(gold_avg, 4),
            # 金价拉不到就是 null，不再拿 0 当现价算出"亏光"的 pnl
            "now_cny_per_gram": round(gold_now, 2) if gold_now else None,
            "value_cny": round(gold_now * grams, 2) if gold_now else None,
            "pnl_cny": round((gold_now - gold_avg) * grams, 2) if gold_now and gold_avg else None,
            "pnl_pct": round(((gold_now / gold_avg) - 1) * 100, 2) if gold_now and gold_avg > 0 else None,
        }

    return {
        "user": {
            "name": pm.user.get("display_name") if pm.user else "unknown",
            "risk_tolerance": pm.user.get("risk_tolerance") if pm.user else None,
        },
        "cash": {
            "cny": round(cash_cny, 2),
            **({
                "aud": round(pm.cash_amount("AUD"), 2),
                "aud_in_cny": round(pm.cash_amount("AUD") * audcny, 2) if audcny else None,
            } if "AUD" in ccys else {}),
            # v2 通用：列出所有币种（其他 agent 想读非 CNY/AUD 时方便）
            "all_currencies": pm.cash,
        },
        **({"gold": gold_block} if gold_block else {}),
        # v2 新增：完整 holdings 数组（其他 yfinance symbol 也能被 agent 看到）
        "all_holdings": holding_views,
        "total_assets_cny": total_cny,
        # 真实账户业绩：twr_30d / twr_ytd / twr_all / xirr / max_drawdown / volatility_annual /
        # benchmark / data_quality（基准币种）；历史不足 → {"status": "insufficient_history"}
        "performance": performance,
        # 拉不到汇率 / 价格、没算进 total 的条目——有就列出来，别让总资产悄悄偏小
        **({"total_assets_excludes": unvalued(_value_status)} if unvalued(_value_status) else {}),
        "fx": {f"{c.lower()}cny": round(r, 4) for c, r in fx_rates.items() if r},
        "live_prices": {
            "gold_usd_per_oz": (gold_extra or {}).get("gold_usd_per_oz"),
            "usdcny": (gold_extra or {}).get("usdcny_rate"),
        } if gold_block else {},
    }


# ---------- strategy ----------

def build_strategy_view() -> Dict[str, Any]:
    store = MemoryStore()
    strat = store.read("strategy")
    insights_dir = store.root / "insights"
    insights = []
    if insights_dir.exists():
        for f in sorted(insights_dir.glob("*.md")):
            doc = store.read(f"insights/{f.stem}")
            if doc:
                insights.append({
                    "slug": f.stem,
                    **{k: v for k, v in doc.metadata.items()
                       if k not in {"name", "type", "updated"}},
                })
    return {
        "strategy": dict(strat.metadata) if strat else None,
        "long_term_insights": insights,
        "insights_count": len(insights),
    }


# ---------- history ----------

def build_history_view(n: int) -> Dict[str, Any]:
    store = MemoryStore()
    trades = sorted(
        store.read_history(),
        key=lambda t: t.get("ts_origin", t.get("ts", "")),
        reverse=True,
    )[:n]

    debates = []
    debate_dir = store.root / ".debate"
    if debate_dir.exists():
        for date_dir in sorted(debate_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for md in date_dir.glob("*.md"):
                content = md.read_text(encoding="utf-8")
                debates.append({
                    "date": date_dir.name,
                    "asset": md.stem,
                    "summary": "\n".join(content.splitlines()[:8]),
                })
            if len(debates) >= n:
                break

    return {"recent_trades": trades, "recent_debates": debates[:n]}


# ---------- what_if ----------

def build_what_if_view(
    *,
    symbol: Optional[str] = None,
    pct: Optional[float] = None,
    price: Optional[float] = None,
    gold_price: Optional[float] = None,
    gold_pct: Optional[float] = None,
    ndq_price: Optional[float] = None,
    ndq_pct: Optional[float] = None,
    audcny: Optional[float] = None,
) -> Dict[str, Any]:
    """通用化情景模拟：支持任意 yfinance symbol 涨跌 X% 后总市值变化

    新接口 (P1-F 修复)：symbol + pct/price 任意持仓涨跌；旧接口（gold_pct /
    ndq_pct / audcny）兼容保留。
    """
    from openinvest.utils.gold_price import get_gold_snapshot
    from openinvest.core.portfolio_manager import PortfolioManager
    try:
        pm = PortfolioManager()
    except FileNotFoundError as e:
        return {
            "status": "error",
            "error": str(e),
            "hint": "memory 还没初始化。先跑 `run.sh init` 完成 onboarding。",
        }

    from openinvest.utils.fx import get_fx_rate, to_base

    snap = get_gold_snapshot(offset_pct=0.0)
    # 拉不到就是拉不到：0 价 / None 汇率在下面按"未计入"处理，不再用 ¥1000/克、4.9 这种编的数
    cur_gold = snap.spot_cny_per_gram if snap else 0.0
    cur_audcny = get_fx_rate("AUD", "CNY")

    # 通用价格 dict：每个 holding 的当前价 + 情景价
    cur_prices: Dict[str, float] = {}
    new_prices: Dict[str, float] = {}
    for h in pm.holdings:
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        if str(h.get("kind")) == "metal":
            cur_prices[sym] = cur_gold
        else:
            cur_prices[sym] = _safe_close(sym) or 0.0   # 本函数内 0 = 拉不到 → 下游 _value_in_cny 记「未计入」
        new_prices[sym] = cur_prices[sym]

    # 应用 --symbol/--pct/--price 通用参数
    # --pct/--price 必须配 --symbol：没 symbol 时静默忽略会让 `what_if --pct -5`
    # 返回 delta=0 的"ok"，是个自信的错误答案（CR 命中）——显式报错要求指定标的。
    if symbol is None and (pct is not None or price is not None):
        return {
            "status": "error",
            "error": "--pct / --price 需要配合 --symbol 指定标的",
            "hint": "如 `what_if --symbol NDQ.AX --pct -5`；组合级情景（全仓齐跌）暂不支持",
        }
    if symbol:
        if symbol not in cur_prices:
            return {
                "status": "error",
                "error": f"{symbol} 不在持仓里",
                "hint": f"用 `run.sh status` 看你有哪些持仓；或先用 GUI / `POST /api/holdings/{symbol}` 加进去再跑 what_if",
            }
        if price is not None:
            new_prices[symbol] = float(price)
        elif pct is not None:
            new_prices[symbol] = cur_prices[symbol] * (1 + pct / 100)

    # 兼容老 --gold-pct / --gold-price
    if gold_price is not None or gold_pct is not None:
        new_gold = cur_gold
        if gold_price is not None:
            new_gold = gold_price
        if gold_pct is not None:
            new_gold = cur_gold * (1 + gold_pct / 100)
        # 应用到所有 metal holdings
        for h in pm.holdings:
            if str(h.get("kind")) == "metal":
                sym = str(h.get("symbol") or "")
                if sym:
                    new_prices[sym] = new_gold

    # 兼容老 --ndq-pct / --ndq-price
    if ndq_price is not None or ndq_pct is not None:
        if "NDQ.AX" in cur_prices:
            new_ndq = cur_prices["NDQ.AX"]
            if ndq_price is not None:
                new_ndq = ndq_price
            if ndq_pct is not None:
                new_ndq = cur_prices["NDQ.AX"] * (1 + ndq_pct / 100)
            new_prices["NDQ.AX"] = new_ndq

    new_audcny = audcny if audcny else cur_audcny

    def _value_in_cny(holding: Dict[str, Any], h_price: float, fx: Optional[float]) -> Optional[float]:
        """折算 holding 当前情景下 CNY 市值。

        2026-05-19 (A5 修复)：之前 if ccy == "AUD" 用 fx，其他币种当 1:1 → USD/EUR
        持仓 what_if 少乘 ~7 倍 / ~7.7 倍。改成 utils.fx.to_base 折算所有币种。
        AUD 特殊：caller 传入的 fx 是情景汇率（用户可指定 --audcny 模拟），优先
        用这个覆盖；其他币种走实时汇率（情景模拟暂不支持多 FX 联动调）。
        价 ≤0（拉取失败的 0.0 哨兵）或汇率拉不到 → None，由调用方列进"未计入"，
        不再按 1:1 把外币金额当人民币加进总额。
        """
        units = float(holding.get("units", 0) or 0)
        ccy = str(holding.get("cost_currency", "CNY")).upper()
        if not h_price or h_price <= 0:
            return None
        local_value = units * h_price
        if ccy == "AUD":
            return local_value * fx if fx else None   # 情景汇率覆盖
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
        return to_base(ccy, local_value, "CNY")

    excluded: List[str] = []
    cur_total = new_total = 0.0
    for ccy, amt in pm.cash.items():
        amt = float(amt or 0)
        if not amt:
            continue
        ccy = str(ccy).upper()
        cur_c = amt * cur_audcny if ccy == "AUD" and cur_audcny else (
            None if ccy == "AUD" else to_base(ccy, amt, "CNY"))
        new_c = amt * new_audcny if ccy == "AUD" and new_audcny else (
            None if ccy == "AUD" else cur_c)
        if cur_c is None or new_c is None:
            excluded.append(f"{ccy} 现金")
            continue
        cur_total += cur_c
        new_total += new_c
    breakdown: Dict[str, Any] = {}
    for h in pm.holdings:
        if h.get("is_tracking_only"):
            continue
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        cur_v = _value_in_cny(h, cur_prices.get(sym, 0.0), cur_audcny)
        new_v = _value_in_cny(h, new_prices.get(sym, 0.0), new_audcny)
        if cur_v is None or new_v is None:
            excluded.append(sym)
            breakdown[sym] = {"units": float(h.get("units", 0) or 0), "unavailable": "拉不到价格或汇率"}
            continue
        cur_total += cur_v
        new_total += new_v
        breakdown[sym] = {
            "units": float(h.get("units", 0) or 0),
            "cur_price": round(cur_prices.get(sym, 0.0), 4),
            "scenario_price": round(new_prices.get(sym, 0.0), 4),
            "cur_value_cny": round(cur_v, 2),
            "scenario_value_cny": round(new_v, 2),
            "delta_cny": round(new_v - cur_v, 2),
        }

    delta = new_total - cur_total
    return {
        "status": "ok",
        "current_total_cny": round(cur_total, 2),
        "scenario_total_cny": round(new_total, 2),
        "delta_cny": round(delta, 2),
        "delta_pct": round((delta / cur_total) * 100, 2) if cur_total else 0.0,
        "fx": {"audcny_cur": round(cur_audcny, 4) if cur_audcny else None,
               "audcny_scenario": round(new_audcny, 4) if new_audcny else None},
        "breakdown": breakdown,
        # 没算进两个总额的条目：有就明说，总额只代表其余部分
        **({"excluded_from_total": excluded} if excluded else {}),
    }


# ---------- doctor ----------

# `init --from-stdin` 的 payload 形状——单一可信源：doctor 的 needs_setup hint 和
# cmd_init 拒收扁平 payload 时回的 expected_shape 都用它（以前两处各写一份，doctor 那份漂成了扁平）
INIT_PAYLOAD_SHAPE: Dict[str, Any] = {
    "profile": {
        "name": "<display name>",
        "risk_tolerance": "Conservative|Balanced|Aggressive",
        "holdings_description": "<用户原话描述持仓，有 LLM key 时后端解析>",
        "current_assets": {"cash_cny": 0, "aud_cash": 0},
        "investment_strategy": {"max_single_invest_cny": 10000},
    },
    "env": {"LLM_API_KEY": "<optional>", "EMAIL_SENDER": "<optional>", "EMAIL_PASSWORD": "<optional>"},
}


def build_doctor_view(root: Path) -> Dict[str, Any]:
    """健康自检：onboarding 是否完成？所有外部依赖可达？

    给 Claude 看的 JSON：每一项是 ok / missing / needs_migration / optional_*，附 hint 教 Claude
    怎么修。让 Claude 第一次帮用户跑 status 失败时，先 doctor 看到底差什么，
    再决定走 AskUserQuestion 还是直接 init。

    总 status 只看 onboarding 必需项（memory 未初始化 / portfolio 是 v1 旧格式 → needs_setup）；
    LLM key / 邮件凭据 / .env 文件缺失是 **optional_***：Coordinator 路径本来就不要 LLM key（#444），
    缺了不该让 agent 切去 invest-setup 重装——只有 Direct 路径（direct_ready）要 key。

    Args:
        root: 项目根（CLI 传 scripts.skill.ROOT——测试会 patch 它；web_api 传自己的 repo root）
    """
    import json
    import os

    from openinvest.core.portfolio_manager import is_v1_portfolio, v1_migrate_command

    checks: List[Dict[str, Any]] = []

    # 1) memory/ 是否已 onboarding
    store = MemoryStore()
    user_doc = store.read("user")
    portfolio_doc = store.read("portfolio")
    strategy_doc = store.read("strategy")
    memory_ok = bool(user_doc and portfolio_doc and strategy_doc)
    checks.append({
        "name": "memory_initialized",
        "status": "ok" if memory_ok else "missing",
        "detail": (
            "memory/{user,strategy,portfolio}.md 全部就绪"
            if memory_ok else
            "缺 memory/user.md（或 strategy / portfolio）—— 用户还没 onboarding"
        ),
        "hint": (
            None if memory_ok else
            "向用户问名字 / 风险偏好 / 持仓（一句话原话）/ 现金 / LLM key（可选，Coordinator "
            "路径不需要）后调 `run.sh init --from-stdin`（详细流程见 "
            "skills/invest/references/onboarding.md）。stdin 必须是这个嵌套 JSON（扁平的会被拒）："
            + json.dumps(INIT_PAYLOAD_SHAPE, ensure_ascii=False)
            + "。用户说的现金一定填进 profile.current_assets（没 LLM key 或解析失败时只有它落库）。"
            "target_assets 留空也行；用户用系统前就持有的仓位之后用 `buy --existing-position` 补（不扣现金）。"
        ),
    })

    # v1 扁平 portfolio.md（#191 修复前的 init 写的 cash_cny/aud_cash）：PortfolioManager 只读
    # v2 cash{}，status 现金恒为 0。init --force 不会转换它（migrate 有 run-once 闸），给确切转换命令。
    portfolio_v1 = portfolio_doc is not None and is_v1_portfolio(getattr(portfolio_doc, "metadata", portfolio_doc))
    migrate_cmd = v1_migrate_command(store)
    checks.append({
        "name": "portfolio_schema",
        "status": "needs_migration" if portfolio_v1 else "ok",
        "detail": (
            "memory/portfolio.md 是 v1 旧格式（扁平 cash_cny/aud_cash）——status 读不到现金，显示 0"
            if portfolio_v1 else "v2（或尚未 onboarding）"
        ),
        "hint": (
            f"跑 `{migrate_cmd}` 转成 v2（幂等，先自动备份 portfolio.md.bak.<时间戳>）；"
            "别为这个重跑 init（--force 也不会转换它）。"
            if portfolio_v1 else None
        ),
    })

    # 2) .env 凭据（全是可选项：Coordinator 路径零凭据，Direct 路径才要 LLM key，邮件只影响 daily_report 发信）
    env_path = root / ".env"
    # LLM_API_KEY 优先；兼容老 fork 用户的 DEEPSEEK_API_KEY
    has_llm_key = bool(os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))
    has_email_sender = bool(os.getenv("EMAIL_SENDER"))
    has_email_pass = bool(os.getenv("EMAIL_PASSWORD"))
    has_email = has_email_sender and has_email_pass
    checks.append({
        "name": ".env_file",
        "status": "ok" if env_path.exists() else "optional_for_coordinator",
        "detail": str(env_path) if env_path.exists() else f"{env_path} 不存在",
        "hint": (
            None if env_path.exists() else
            f"可选：Direct 路径 / 发邮件才需要凭据，让用户自己在 {env_path} 写 LLM_API_KEY= / EMAIL_SENDER= / "
            "EMAIL_PASSWORD=（不要在对话里收集密钥），或在自己的终端里跑 `run.sh init`；也可直接设进进程环境变量。"
        ),
    })
    checks.append({
        "name": "llm_key",
        "status": "ok" if has_llm_key else "optional_for_coordinator",
        "detail": (
            "LLM_API_KEY / DEEPSEEK_API_KEY 已设" if has_llm_key
            else "LLM_API_KEY / DEEPSEEK_API_KEY 均缺失"
        ),
        "hint": (
            None if has_llm_key else
            "可选：只有 Direct 路径（run_committee / 无人值守 cron）需要；Coordinator 路径（prepare_committee + "
            "spawn 4 subagent）不需要，别为这个重装。要用 Direct 时让用户自己把 LLM_API_KEY=sk-xxxx 写进 "
            f"{env_path}（不要在对话里收集）；默认 DeepSeek（https://platform.deepseek.com），"
            "换 OpenAI 兼容的其他厂商再设 LLM_BASE_URL / LLM_MODEL。"
        ),
    })
    checks.append({
        "name": "gmail_credentials",
        "status": "ok" if has_email else "optional_for_email",
        "detail": (
            f"sender={os.getenv('EMAIL_SENDER')}, password set"
            if has_email else
            "EMAIL_SENDER 或 EMAIL_PASSWORD 缺失"
        ),
        "hint": (
            None if has_email else
            "可选：只影响 daily_report 发邮件（未配置时日报仍能跑完，只是不发信），与 Coordinator / Direct 路径无关。"
            "要发信时 Gmail 必须用 App Password（不是登录密码），需先开 2FA 然后去 "
            f"https://myaccount.google.com/apppasswords 生成 16 位 App Password，用户自己写进 {env_path}"
            "（不要在对话里收集）。"
        ),
    })

    # 3) LLM key 实测可达（audit PM Major: 失败前置）
    # 用统一的 utils.llm 读 base_url/key，支持 LLM_* 切千问/智谱
    llm_reachable = "skipped"
    llm_detail = "LLM_API_KEY / DEEPSEEK_API_KEY 未设，跳过实测"
    if has_llm_key:
        try:
            import requests
            from openinvest.utils.llm import get_llm_config_safe
            _llm_key, _llm_base, _llm_model, _llm_provider = get_llm_config_safe()
            # base_url 可能已含 /v1（如 MiMo Token Plan），也可能不含（如 DeepSeek 默认）
            _base_clean = _llm_base.rstrip("/")
            _models_path = "/models" if _base_clean.endswith("/v1") else "/v1/models"
            r = requests.get(
                f"{_base_clean}{_models_path}",
                headers={"Authorization": f"Bearer {_llm_key}"},
                timeout=8,
            )
            if r.status_code == 200:
                llm_reachable = "ok"
                llm_detail = f"LLM API ({_llm_base}) 响应 200，key 有效"
            elif r.status_code == 401:
                llm_reachable = "auth_failed"
                llm_detail = f"LLM API ({_llm_base}) 返回 401，key 无效或已过期"
            else:
                llm_reachable = "unreachable"
                llm_detail = f"LLM API ({_llm_base}) 返回 HTTP {r.status_code}"
        except Exception as e:
            llm_reachable = "network_error"
            llm_detail = f"无法连接 LLM API: {type(e).__name__}: {e}"
    checks.append({
        "name": "llm_reachable",
        "status": llm_reachable if llm_reachable in ("ok", "skipped") else "optional_for_coordinator",
        "detail": llm_detail,
        "hint": (
            None if llm_reachable in ("ok", "skipped") else
            "只影响 Direct 路径：检查 key 是否被禁用 / 余额不足 / LLM_BASE_URL 是否可达。"
            "失败时仍可用 Coordinator 路径跑 prepare_committee。"
        ),
    })

    # 4) 行情数据库 / cache_data 目录
    db_path = root / "db" / "market_data.db"
    cache_dir = root / "cache_data"
    checks.append({
        "name": "data_dirs",
        "status": "ok",  # Dockerfile 里 mkdir 过，本地脚本也兜底
        "detail": (
            f"db={'exists' if db_path.exists() else 'will_be_created'}, "
            f"cache={'exists' if cache_dir.exists() else 'will_be_created'}"
        ),
        "hint": None,
    })

    # 4) Python venv：git clone 形态的遗留项（跑到这里就证明 venv ok，只报告版本）。
    # uvx / wheel 形态下数据目录里没有 pyproject.toml + skills/（同 paths._detect_root 的 clone 判据），没有 venv 可言 → 跳过
    if (root / "pyproject.toml").exists() and (root / "skills").is_dir():
        checks.append({
            "name": "python_venv",
            "status": "ok",
            "detail": f"running on {sys.version.split()[0]}",
            "hint": None,
        })

    # 5) GUI 壳层已退役（2026-07-05）——web_gui 体检项随之删除，GUI 重做时另起前端

    # 只有 onboarding 必需项（memory 未初始化 / portfolio v1 待迁移）挡路；optional_* / skipped 不算
    blocking = [c["name"] for c in checks
                if c["status"] not in ("ok", "skipped") and not c["status"].startswith("optional")]
    overall = "ready" if not blocking else "needs_setup"

    # ready_for_subcommands 兼容旧字段；新增分路径就绪标志：
    # - coordinator_ready：Claude Code 走 prepare_committee + spawn 4 subagent，
    #   不需要 DeepSeek key（用 Claude 订阅扮演 worker）
    # - direct_ready：任意 agent 走 run_committee，需要 DeepSeek key 跑 4 角色
    # 旧 ready_for_subcommands 之前要求 LLM key，会让 Coordinator 用户被
    # 误判"还没就绪" → agent 反复引导去注册 DeepSeek，体验糟糕
    from openinvest import __version__  # 远端 CLI 靠它判断 hub 是否支持幂等键（#432 评审第 3 条）

    return {
        "status": overall,
        "version": __version__,
        "ready_for_subcommands": memory_ok,  # 等价于 coordinator_ready
        "coordinator_ready": memory_ok,
        "direct_ready": memory_ok and has_llm_key,
        "next_step": (
            "用户已就绪（不要切 invest-setup）。Coordinator 路径（Claude Code / Hermes）直接调 status / "
            "prepare_committee；Direct 路径（Cursor/Cline/Codex/cron）走 run_committee，"
            + ("LLM key 已配置" if has_llm_key else "需要先配 LLM_API_KEY，见 checks 里 llm_key 的 hint（可选项，缺了只是 Direct 路径不可用）")
            if overall == "ready" else
            f"portfolio.md 是 v1 旧格式：先跑 `{migrate_cmd}`，再看其他 status≠ok 的项"
            if portfolio_v1 else
            "调 run.sh init 完成 onboarding，缺什么字段看 checks 里 status='missing' 的项"
        ),
        "checks": checks,
    }
