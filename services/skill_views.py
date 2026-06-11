"""Skill 只读视图构建器 — CLI (scripts/skill.py) 与 Web API (connectors/web_api.py) 共享

2026-06 远端模式（hub-and-spoke）重构：status / strategy / history / what_if / doctor
的计算体从 scripts/skill.py 提取到这里，CLI 和 /api/skill/* 端点都调同一份函数，
防止 local / remote 输出形状漂移（漂移历史见 CLAUDE.md 分层契约段）。

约定：
- 每个 builder 返回 dict（不打印、不 sys.exit），错误也以 {"status": "error", ...}
  dict 返回，由调用方决定退出码 / HTTP 状态码
- 外部数据调用（yfinance / gold / fx）保持**函数内延迟 import**——
  tests/test_onboarding_smoke.py 等用 patch("utils.exchange_fee.get_history_data")
  的测试依赖 call-time 解析
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.memory_store import MemoryStore


def _safe_close(symbol: str) -> float:
    from utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


# ---------- status ----------

def build_status_view() -> Dict[str, Any]:
    """v2 通用化：从 cash dict + holdings list 读，对外保持原 JSON 结构兼容老 agent"""
    from utils.gold_price import get_gold_snapshot
    from core.portfolio_manager import PortfolioManager
    from utils.fx import total_portfolio_value_cny
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
    for h in pm.holdings:
        sym = str(h.get("symbol") or "")
        if not sym or h.get("is_tracking_only"):
            continue
        if sym == "NDQ.AX":
            current_prices[sym] = ndq_price
        elif sym == "GC=F":
            current_prices[sym] = gold_now
        else:
            current_prices[sym] = _safe_close(sym)
    total_cny, _value_status = total_portfolio_value_cny(pm, current_prices, base="CNY")

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
        "all_holdings": [
            {k: h[k] for k in (
                "symbol", "kind", "units", "unit_label", "avg_cost",
                "cost_currency", "channel", "display_name", "is_tracking_only",
            ) if k in h}
            for h in pm.holdings
        ],
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
    from utils.gold_price import get_gold_snapshot
    from core.portfolio_manager import PortfolioManager
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
        from utils.fx import to_base
        units = float(holding.get("units", 0) or 0)
        ccy = str(holding.get("cost_currency", "CNY"))
        local_value = units * h_price
        if ccy == "CNY":
            return local_value
        if ccy == "AUD":
            return local_value * fx   # 情景汇率覆盖
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
            "(Conservative/Balanced/Aggressive), monthly_income_cny / "
            "monthly_expenses_cny / exchange_buffer_cny（都可填 0 跳过），"
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
            from utils.llm import get_llm_config_safe
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

    # 5) GUI dist + 是否在跑
    # GUI 是面向**小白用户**的主入口（CLI/skill 是给 agent 和极客的）。
    # 设计原则：
    #   - dist 在 bootstrap 阶段自动拉好（skills/invest/scripts/run.sh 第一次跑就 sync）
    #   - agent 必须主动告知用户 GUI 入口在哪，否则小白找不到
    #   - 但同一会话只在第一次回答提一次 URL，之后别复读
    # 高级用户（明确不想看 GUI 提示）可以 `INVEST_GUI_NUDGE=off`
    gui_nudge_off = os.getenv("INVEST_GUI_NUDGE", "").strip().lower() in {"off", "0", "false", "no"}
    gui_index = root / "static" / "index.html"
    gui_dist_ready = gui_index.exists()
    gui_port = int(os.getenv("INVEST_WEB_PORT", "8765"))
    gui_host = os.getenv("INVEST_WEB_HOST", "127.0.0.1")
    gui_running = False
    try:
        # 不发 HTTP 请求（怕被 hang），用 socket connect 测端口
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            gui_running = (s.connect_ex((gui_host, gui_port)) == 0)
    except Exception:  # noqa: BLE001
        gui_running = False

    if gui_running:
        gui_status = "ok"
        gui_detail = f"GUI 在跑：http://{gui_host}:{gui_port}"
        gui_hint = None if gui_nudge_off else (
            f"⭐ GUI 在跑，URL: http://{gui_host}:{gui_port}。这是面向小白用户的"
            "主面板（持仓 / 委员会直播 / 历史决议 / LLM 用量）。**第一次回答用户**"
            f"时附一句：顺便：浏览器开 http://{gui_host}:{gui_port} 可以看完整面板。"
            "同会话之后别再复读。"
        )
    elif gui_dist_ready:
        gui_status = "ok"
        gui_detail = f"GUI dist 已装但未启动（端口 {gui_port} 没人监听）"
        gui_hint = None if gui_nudge_off else (
            f"⭐ GUI dist 已就绪但 uvicorn 没起。这是面向**小白用户**的主面板，"
            "不告诉他他就找不到。**第一次回答用户**时附一句：「顺便：项目自带 "
            "Web GUI，想看持仓/委员会面板的话另开终端跑 "
            f"`~/.claude/skills/invest/scripts/run.sh gui`，浏览器开 "
            f"http://{gui_host}:{gui_port}」。同会话别复读。"
        )
    else:
        # dist 没装是 bootstrap 失败的信号——主动帮用户装，不是丢锅给用户
        gui_status = "missing"
        gui_detail = "static/index.html 不存在 — bootstrap 阶段 GUI dist 没拉成功"
        gui_hint = (
            "GUI dist 应该在 skill 第一次跑时自动拉，没拉到大概率是 GitHub 网络问题。"
            "**直接帮用户跑** `cd $INVEST_HOME && uv run python -m scripts.sync_gui_dist` "
            "把 dist 装好（不要让用户自己 google 怎么修）；装好后告诉他 GUI 入口在 "
            f"http://{gui_host}:{gui_port}。"
        )

    checks.append({
        "name": "web_gui",
        "status": gui_status,
        "detail": gui_detail,
        "hint": gui_hint,
        "gui_url": f"http://{gui_host}:{gui_port}" if gui_dist_ready else None,
        "gui_running": gui_running,
        "gui_dist_ready": gui_dist_ready,
        "gui_nudge_off": gui_nudge_off,
    })

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
