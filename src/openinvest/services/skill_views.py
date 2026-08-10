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

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from openinvest.core.memory_store import MemoryStore


def _safe_close(symbol: str) -> float:
    from openinvest.utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


# ---------- status ----------

def build_status_view() -> Dict[str, Any]:
    """v2 通用化：从 cash dict + holdings list 读，对外保持原 JSON 结构兼容老 agent"""
    from openinvest.utils.gold_price import get_gold_snapshot
    from openinvest.core.portfolio_manager import PortfolioManager
    from openinvest.utils.fx import total_portfolio_value_cny
    from openinvest.utils.quotes import get_quote
    pm = PortfolioManager()

    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    ndq_h = pm.holdings.find("NDQ.AX")
    gold_h = pm.holdings.find("GC=F")
    ndq_shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    gold_grams = float(gold_h.get("units", 0) or 0) if gold_h else 0.0
    gold_avg = float(gold_h.get("avg_cost", 0) or 0) if gold_h else 0.0

    ndq_price = _safe_close("NDQ.AX")
    audcny = _safe_close("AUDCNY=X")
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_now = snap.spot_cny_per_gram if snap else 0.0

    # 2026-05-19 (A6 修复): total_assets_cny 之前写死 cash + aud*fx + ndq*price*fx +
    # gold*price，fork 用户的 AAPL/0700.HK/BTC-USD 全漏算。改走 utils.fx.total_portfolio_value_cny
    # 通用化遍历所有 holdings + 多币种 cash 折算到 CNY。
    current_prices: Dict[str, float] = {}
    quote_meta: Dict[str, Dict[str, Any]] = {}
    for h in pm.holdings:
        sym = str(h.get("symbol") or "")
        if not sym or h.get("is_tracking_only"):
            continue
        if sym == "NDQ.AX":
            current_prices[sym] = ndq_price
        elif sym == "GC=F":
            current_prices[sym] = gold_now
        else:
            quote = get_quote(h)
            if quote is not None and math.isfinite(quote.price) and quote.price > 0:
                current_prices[sym] = quote.price
                quote_meta[sym] = {
                    "last_updated": quote.last_updated,
                    "is_stale": quote.is_stale,
                    "source": (quote.extra or {}).get("source", "yfinance"),
                    "extra": quote.extra or {},
                }
    total_cny, _value_status = total_portfolio_value_cny(pm, current_prices, base="CNY")

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
            view.update({
                "current_price": round(price, 6),
                "market_value": round(market_value, 2),
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round((price / avg_cost - 1) * 100, 2) if avg_cost > 0 else None,
                **quote_meta.get(sym, {}),
            })
        else:
            view.update({
                "current_price": None,
                "market_value": None,
                "pnl": None,
                "pnl_pct": None,
            })
        holding_views.append(view)

    return {
        "user": {
            "name": pm.user.get("display_name") if pm.user else "unknown",
            "risk_tolerance": pm.user.get("risk_tolerance") if pm.user else None,
        },
        "cash": {
            "cny": round(cash_cny, 2),
            "aud": round(aud_cash, 2),
            "aud_in_cny": round(aud_cash * audcny, 2),
            # v2 通用：列出所有币种（其他 agent 想读非 CNY/AUD 时方便）
            "all_currencies": pm.cash,
        },
        "ndq": {
            "shares": ndq_shares,
            "price_aud": round(ndq_price, 2),
            "value_cny": round(ndq_shares * ndq_price * audcny, 2),
        },
        "gold": {
            "grams": gold_grams,
            "avg_cost_cny_per_gram": gold_avg,
            "now_cny_per_gram": round(gold_now, 2),
            "value_cny": round(gold_now * gold_grams, 2),
            "pnl_cny": round((gold_now - gold_avg) * gold_grams, 2) if gold_avg else 0,
            "pnl_pct": round(((gold_now / gold_avg) - 1) * 100, 2) if gold_avg > 0 else 0,
        },
        # v2 新增：完整 holdings 数组（其他 yfinance symbol 也能被 agent 看到）
        "all_holdings": holding_views,
        "total_assets_cny": total_cny,
        "fx": {"audcny": round(audcny, 4)},
        "live_prices": {
            "gold_usd_per_oz": snap.gold_usd_per_oz if snap else None,
            "usdcny": snap.usdcny_rate if snap else None,
        },
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

    snap = get_gold_snapshot(offset_pct=0.0)
    cur_gold = snap.spot_cny_per_gram if snap else 1000.0
    cur_audcny = _safe_close("AUDCNY=X") or 4.9

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
            cur_prices[sym] = _safe_close(sym)
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

    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")

    def _value_in_cny(holding: Dict[str, Any], h_price: float, fx: float) -> float:
        """折算 holding 当前情景下 CNY 市值。

        2026-05-19 (A5 修复)：之前 if ccy == "AUD" 用 fx，其他币种当 1:1 → USD/EUR
        持仓 what_if 少乘 ~7 倍 / ~7.7 倍。改成 utils.fx.to_base 折算所有币种。
        AUD 特殊：caller 传入的 fx 是情景汇率（用户可指定 --audcny 模拟），优先
        用这个覆盖；其他币种走实时汇率（情景模拟暂不支持多 FX 联动调）。
        """
        from openinvest.utils.fx import to_base
        units = float(holding.get("units", 0) or 0)
        ccy = str(holding.get("cost_currency", "CNY"))
        local_value = units * h_price
        if ccy == "CNY":
            return local_value
        if ccy == "AUD":
            return local_value * fx   # 情景汇率覆盖
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
        converted = to_base(ccy, local_value, "CNY")
        return converted if converted is not None else local_value  # 拉不到汇率退化

    cur_total = cash_cny + aud_cash * cur_audcny
    new_total = cash_cny + aud_cash * new_audcny
    breakdown: Dict[str, Any] = {}
    for h in pm.holdings:
        if h.get("is_tracking_only"):
            continue
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        cur_v = _value_in_cny(h, cur_prices.get(sym, 0.0), cur_audcny)
        new_v = _value_in_cny(h, new_prices.get(sym, 0.0), new_audcny)
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
        "fx": {"audcny_cur": round(cur_audcny, 4), "audcny_scenario": round(new_audcny, 4)},
        "breakdown": breakdown,
    }


# ---------- doctor ----------

def build_doctor_view(root: Path) -> Dict[str, Any]:
    """健康自检：onboarding 是否完成？所有外部依赖可达？

    给 Claude 看的 JSON：每一项是 ok / missing / unreachable，附 hint 教 Claude
    怎么修。让 Claude 第一次帮用户跑 status 失败时，先 doctor 看到底差什么，
    再决定走 AskUserQuestion 还是直接 init。

    Args:
        root: 项目根（CLI 传 scripts.skill.ROOT——测试会 patch 它；web_api 传自己的 repo root）
    """
    import os

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
            "向用户问以下信息后调 `run.sh init --from-stdin`（详细流程见 "
            "skills/invest/references/onboarding.md）：display_name, risk_tolerance "
            "(Conservative/Balanced/Aggressive), "
            "holdings_description（自由描述持仓，例如 '510300 沪深300ETF "
            "3000 股 4.2 元，余额宝 5 万 CNY'），DEEPSEEK_API_KEY（可选，"
            "Coordinator 路径不需要）。target_assets 留空也行，onboarding "
            "完用户可以通过 GUI 或 references/adding-assets.md 加任意 yfinance symbol。"
        ),
    })

    # 2) .env 凭据
    env_path = root / ".env"
    # LLM_API_KEY 优先；兼容老 fork 用户的 DEEPSEEK_API_KEY
    has_deepseek = bool(os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY"))
    has_email_sender = bool(os.getenv("EMAIL_SENDER"))
    has_email_pass = bool(os.getenv("EMAIL_PASSWORD"))
    checks.append({
        "name": ".env_file",
        "status": "ok" if env_path.exists() else "missing",
        "detail": str(env_path) if env_path.exists() else f"{env_path} 不存在",
        "hint": (
            None if env_path.exists() else
            "调 `run.sh init` 时把 deepseek_api_key / email_sender / email_password "
            "写在 stdin JSON 里，或者直接 cp .env.example .env 后用户自己填"
        ),
    })
    checks.append({
        "name": "deepseek_key",
        "status": "ok" if has_deepseek else "missing",
        "detail": (
            "LLM_API_KEY / DEEPSEEK_API_KEY 已设" if has_deepseek
            else "LLM_API_KEY / DEEPSEEK_API_KEY 均缺失"
        ),
        "hint": (
            None if has_deepseek else
            "向用户引导：去 https://platform.deepseek.com 注册 → API keys 页面创建 "
            "→ 把 sk-xxxx 通过 init 的 stdin 传入。失败时仍可用 Claude skill 模式"
            "（不需要 DeepSeek key），但 cron 模式无法跑。"
        ),
    })
    checks.append({
        "name": "gmail_credentials",
        "status": "ok" if (has_email_sender and has_email_pass) else "missing",
        "detail": (
            f"sender={os.getenv('EMAIL_SENDER')}, password set"
            if (has_email_sender and has_email_pass) else
            "EMAIL_SENDER 或 EMAIL_PASSWORD 缺失"
        ),
        "hint": (
            None if (has_email_sender and has_email_pass) else
            "Gmail 必须用 App Password（不是登录密码），需先开 2FA 然后去 "
            "https://myaccount.google.com/apppasswords 生成 16 位 App Password。"
            "未配置时 daily_report 仍能跑完，只是不发邮件。"
        ),
    })

    # 3) LLM key 实测可达（audit PM Major: 失败前置）
    # 用统一的 utils.llm 读 base_url/key，支持 LLM_* 切千问/智谱
    deepseek_reachable = "skipped"
    deepseek_detail = "LLM_API_KEY / DEEPSEEK_API_KEY 未设，跳过实测"
    if has_deepseek:
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
                deepseek_reachable = "ok"
                deepseek_detail = f"LLM API ({_llm_base}) 响应 200，key 有效"
            elif r.status_code == 401:
                deepseek_reachable = "auth_failed"
                deepseek_detail = f"LLM API ({_llm_base}) 返回 401，key 无效或已过期"
            else:
                deepseek_reachable = "unreachable"
                deepseek_detail = f"LLM API ({_llm_base}) 返回 HTTP {r.status_code}"
        except Exception as e:
            deepseek_reachable = "network_error"
            deepseek_detail = f"无法连接 LLM API: {type(e).__name__}: {e}"
    checks.append({
        "name": "deepseek_reachable",
        "status": deepseek_reachable if deepseek_reachable in ("ok", "skipped") else "missing",
        "detail": deepseek_detail,
        "hint": (
            None if deepseek_reachable in ("ok", "skipped") else
            "去 https://platform.deepseek.com 检查 key 是否被禁用 / 余额不足。"
            "失败时仍可用 Claude skill 模式跑 prepare_committee。"
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

    # 4) Python venv（skill 本身能跑到这里就证明 venv ok，但报告上有更友好）
    checks.append({
        "name": "python_venv",
        "status": "ok",
        "detail": f"running on {sys.version.split()[0]}",
        "hint": None,
    })

    # 5) GUI 壳层已退役（2026-07-05）——web_gui 体检项随之删除，GUI 重做时另起前端

    overall = "ready" if all(c["status"] == "ok" for c in checks) else "needs_setup"

    # ready_for_subcommands 兼容旧字段；新增分路径就绪标志：
    # - coordinator_ready：Claude Code 走 prepare_committee + spawn 4 subagent，
    #   不需要 DeepSeek key（用 Claude 订阅扮演 worker）
    # - direct_ready：任意 agent 走 run_committee，需要 DeepSeek key 跑 4 角色
    # 旧 ready_for_subcommands 之前要求 has_deepseek，会让 Coordinator 用户被
    # 误判"还没就绪" → agent 反复引导去注册 DeepSeek，体验糟糕
    return {
        "status": overall,
        "ready_for_subcommands": memory_ok,  # 等价于 coordinator_ready
        "coordinator_ready": memory_ok,
        "direct_ready": memory_ok and has_deepseek,
        "next_step": (
            "用户已就绪。Claude Code 用户直接调 status / prepare_committee；"
            "其他 agent（Cursor/Cline/Codex）走 run_committee（需 DEEPSEEK_API_KEY）"
            if overall == "ready" else
            "调 run.sh init 完成 onboarding，缺什么字段看 checks 里 status='missing' 的项"
        ),
        "checks": checks,
    }
