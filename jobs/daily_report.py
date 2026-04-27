"""每日投资报告 - 多资产并发分析

仿 OpenClaw cron job 入口约定：模块级 `run()` 返回执行结果。
被 scheduler.runner 通过 jobs/daily_report.yml 的 entry 配置触发。

流程：
1. 读 memory/strategy.md 拿 target_assets 列表
2. 并发跑：macro agent + 每个 asset 的 stock/gold agent + forex agent (有非 CNY 资产时)
3. manager 综合所有意见给最终决策
4. 黄金部分单独算克价（utils.gold_price）
5. 决策 + 市场数据 append 到 memory/daily/<date>.md（供 dreaming 用）
6. 拼报告发邮件
"""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from agents.agent import SimpleAgent
from agents.forex import PROMPT_FOREX_AGENT
from agents.gold import build_gold_prompt
from agents.macro import PROMPT_MACRO_AGENT
from agents.manager import build_manager_prompt
from agents.stock import build_stock_prompt
from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from services.notifier import send_gmail_notification
from utils.exchange_fee import (
    analyze_multi_timeframe,
    get_cost_report,
    get_history_data,
    get_macro_data,
)
from utils.gold_price import format_gold_report, get_gold_snapshot

load_dotenv()


def _get_agent_config() -> Dict[str, Optional[str]]:
    return {
        "deepseek_api_key": os.getenv("DEEPSEEK_API_KEY"),
        "deepseek_base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    }


def _create_agent(system_prompt: str, model_name: str = "deepseek-chat") -> Optional[SimpleAgent]:
    cfg = _get_agent_config()
    if not cfg["deepseek_api_key"]:
        print("❌ DEEPSEEK_API_KEY 缺失")
        return None
    return SimpleAgent(
        temperature=0.1,
        enable_search=True,
        model=model_name,
        openai_api_key=cfg["deepseek_api_key"],
        openai_api_base=cfg["deepseek_base_url"],
        system_prompt=system_prompt,
        debug=False,
    )


def _get_last_close(symbol: str, label: str) -> float:
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        print(f"⚠️ {label} 数据缺失: {symbol}")
        return 0.0
    return float(df["Close"].iloc[-1])


def _is_china_market(symbol: str) -> bool:
    suffix = symbol.upper().split(".")[-1]
    return suffix in {"SZ", "SS", "BJ", "HK"}


def _run_gemini_cli_review(prompt: str) -> str:
    """通过 stdin 把 prompt 喂给 gemini CLI（修复了之前 argv 泄漏问题）"""
    print("🤖 [Gemini CLI] 正在生成第二意见...")
    gemini_cmd = "/home/ubuntu/.nvm/versions/node/v24.13.0/bin/gemini"
    if not os.path.exists(gemini_cmd):
        gemini_cmd = "gemini"
    try:
        result = subprocess.run(
            [gemini_cmd],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "Skipped: gemini CLI 不可用"
    except Exception as e:
        return f"Skipped: {e}"


def _build_asset_job(asset: Dict[str, Any]) -> Dict[str, Any]:
    """根据 asset 配置返回一个 job 描述（prompt + label + 市场数据查询）"""
    symbol = asset["symbol"]
    asset_type = asset.get("type", "equity_etf")

    if asset_type == "metal":
        prompt = build_gold_prompt(symbol)
        emoji = "🪙"
    else:
        prompt = build_stock_prompt(symbol)
        emoji = "📈"

    return {
        "asset": asset,
        "symbol": symbol,
        "type": asset_type,
        "display_name": asset.get("display_name", symbol),
        "prompt": prompt,
        "preview_label": f"✅ {emoji} {asset.get('display_name', symbol)} 观点",
        "error_log_label": f"Asset Agent ({symbol})",
        "failed_msg": f"⚠️ **Analysis Failed**: {symbol} agent encountered an error.",
        "unavailable_title": f"{symbol} Analysis",
    }


def _run_agent_job(job: Dict[str, Any], query: str) -> tuple[str, str]:
    """跑单个 agent，返回 (analysis, tool_context)"""
    analysis = job["failed_msg"]
    context = ""
    try:
        agent = _create_agent(job["prompt"])
        if agent:
            analysis = agent.run(query)
            context = agent.get_context()
            print(f"{job['preview_label']}:\n{analysis[:150]}...")
    except Exception as e:
        print(f"❌ [Error] {job['error_log_label']} failed: {e}")
        analysis = f"⚠️ **{job['unavailable_title']} Unavailable**\n\nError: {e}"
    return analysis, context


def _build_market_data_for_asset(asset: Dict[str, Any]) -> str:
    """为单个 asset 拉历史数据 + 多周期分析"""
    symbol = asset["symbol"]
    label = f"{asset.get('display_name', symbol)} ({symbol})"
    return analyze_multi_timeframe(get_history_data(symbol, "2y"), label)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def run() -> Dict[str, Any]:
    """scheduler / NapCat / main.py 都通过这个入口触发"""
    pm = PortfolioManager()
    store = pm.store
    today = datetime.now().strftime("%Y-%m-%d")

    # --- 1. 读策略与持仓 ---
    target_assets = list(pm.strategy.get("target_assets", []))
    if not target_assets:
        print("⚠️ strategy.target_assets 为空，跳过")
        return {"status": "skipped", "reason": "no_target_assets"}

    # --- 2. 拉关键价格用于估值 ---
    primary_symbol = target_assets[0]["symbol"]
    current_price = _get_last_close(primary_symbol, "主资产")
    current_rate = _get_last_close("AUDCNY=X", "汇率")

    user_status = pm.get_user_status(current_price, current_rate)

    # --- 3. 黄金克价快照 ---
    gold_asset = next((a for a in target_assets if a.get("type") == "metal"), None)
    gold_offset = float(gold_asset.get("price_offset_pct", 0.015)) if gold_asset else 0.015
    gold_snap = get_gold_snapshot(offset_pct=gold_offset)
    gold_report = format_gold_report(gold_snap) if gold_snap else "黄金数据获取失败"

    # --- 4. 给每个 asset 拼市场数据报告 ---
    print("📊 正在生成每个资产的市场数据报告...")
    asset_market_reports = {a["symbol"]: _build_market_data_for_asset(a) for a in target_assets}

    # --- 5. 宏观数据 ---
    print("🌍 获取宏观数据...")
    macro_data_report = get_macro_data()

    # --- 6. 是否有需要换汇的非 CNY 资产 ---
    has_non_cny_asset = any(a.get("currency", "CNY") != "CNY" for a in target_assets)
    is_china_only = not has_non_cny_asset

    # --- 7. 摩擦成本（只针对 NDQ 等需要换汇的） ---
    if has_non_cny_asset:
        friction_report = get_cost_report(
            invest_cny=user_status.disposable_for_invest,
            spot_rate=current_rate,
        )
    else:
        friction_report = "N/A (本期无需换汇)"

    # --- 8. 准备 jobs：macro + 每个 asset + 可选 forex ---
    jobs: Dict[str, Dict[str, Any]] = {
        "macro": {
            "prompt": PROMPT_MACRO_AGENT,
            "preview_label": "🌍 宏观策略师观点",
            "error_log_label": "Agent Macro",
            "failed_msg": "⚠️ **Analysis Failed**: Macro agent encountered an error.",
            "unavailable_title": "Macro Analysis",
            "query": f"# Macro Data Reference:\n{macro_data_report}\n\n"
                     f"Please analyze the global macro environment "
                     f"(Interest Rates, Inflation, Cycle, Geopolitics).",
        },
    }

    for asset in target_assets:
        symbol = asset["symbol"]
        asset_job = _build_asset_job(asset)
        asset_job["query"] = (
            f"# target asset: {asset.get('display_name', symbol)} ({symbol})\n"
            f"# channel: {asset.get('channel', 'N/A')}\n"
            f"# market data:\n{asset_market_reports[symbol]}\n\n"
            f"Please analyze the trend and provide buying recommendations."
        )
        jobs[f"asset:{symbol}"] = asset_job

    if has_non_cny_asset:
        fx_report = analyze_multi_timeframe(
            get_history_data("AUDCNY=X", "2y"), "CURRENCY RATE (AUD/CNY)"
        )
        jobs["fx"] = {
            "prompt": PROMPT_FOREX_AGENT,
            "preview_label": "💱 外汇专家观点",
            "error_log_label": "Agent FX",
            "failed_msg": "⚠️ **Analysis Failed**: Forex agent encountered an error.",
            "unavailable_title": "Forex Analysis",
            "query": f"# market data:\n{fx_report}\n\n{friction_report}\n\n"
                     f"Please analyze AUD/CNY trend and exchange recommendations.",
        }
    else:
        fx_report = "N/A"

    # --- 9. 串行跑所有 agent ---
    # 注意：原来用 ThreadPoolExecutor 并发，但 trafilatura/lxml 是 C 扩展
    # 多线程会触发 libxml2 全局状态竞争 → "free(): invalid pointer" core dump。
    # 改成串行慢一点（~3 分钟）但稳定。后续可以改 ProcessPool 提速。
    print(f"\n🤖 串行执行 {len(jobs)} 个 agent (避免 lxml 多线程崩溃)...")
    results: Dict[str, str] = {}
    contexts: Dict[str, str] = {}
    for key, job in jobs.items():
        ans, ctx = _run_agent_job(job, job["query"])
        results[key] = ans
        contexts[key] = ctx

    macro_analysis = results.get("macro", "")
    fx_analysis = results.get("fx", "⚠️ **Forex Analysis Skipped** (CNY-only assets)")
    asset_analyses = {
        a["symbol"]: results.get(f"asset:{a['symbol']}", "") for a in target_assets
    }

    # --- 10. Manager 综合决策 ---
    print("\n🤖 [Chief Manager] 综合决策中...")
    asset_block = "\n\n".join([
        f"### {a.get('display_name', a['symbol'])} ({a['symbol']})\n{asset_analyses[a['symbol']]}"
        for a in target_assets
    ])
    final_prompt = f"""
你是一名专业的私人投资顾问。综合以下信息给出决策：

1. **用户画像**: 风险偏好【{user_status.risk_level}】，
   现金 ¥{user_status.cash_cny:,.0f}，
   AUD ${user_status.cash_aud:,.0f}，
   本期最大可投预算 ¥{user_status.disposable_for_invest:,.0f}。
2. **宏观策略师观点**: {macro_analysis}
3. **外汇专家观点**: {fx_analysis}
4. **资产分析**:
{asset_block}
5. **黄金价格快照**:
{gold_report}
6. **交易摩擦成本**:
{friction_report}

**任务**：
- 必须考虑宏观因素的最高权重（macro_score < 0 时强制保守）
- 用户当前状态：AUD 子弹已基本投完 NDQ，黄金可继续买克
- 给每个 target asset 一个明确建议（**投资金额 CNY** + **买/持/卖**）
- 总结性陈述当前的"风口/雷区"判断
"""

    manager_prompt = build_manager_prompt("multi-asset")
    final_decision_ds = "⚠️ Decision Failed (DeepSeek)"
    final_decision_gemini = "⚠️ Decision Failed (Gemini)"

    try:
        agent_manager = _create_agent(manager_prompt)
        if agent_manager:
            final_decision_ds = agent_manager.run(final_prompt)
    except Exception as e:
        final_decision_ds = f"⚠️ DeepSeek error: {e}"

    # Gemini 用 stdin 喂 prompt（修了 argv 泄漏）
    try:
        gemini_prompt = (
            final_prompt
            + "\n\n---\n请用搜索工具验证最新汇率/价格，再给最终建议。"
        )
        final_decision_gemini = _run_gemini_cli_review(gemini_prompt)
    except Exception as e:
        final_decision_gemini = f"⚠️ Gemini error: {e}"

    # --- 11. 拼最终报告 ---
    asset_section = "\n\n---\n\n".join([
        f"## {idx+2}. {a.get('display_name', a['symbol'])} ({a['symbol']}) 分析\n"
        f"{asset_analyses[a['symbol']]}"
        for idx, a in enumerate(target_assets)
    ])
    full_report = f"""
# 投资分析报告 / Invest Agent Report ({today})

## 1. 宏观策略环境
{macro_analysis}

---

## 黄金价格快照 (浙商积存金参考)
```
{gold_report}
```

---

{asset_section}

---

## {len(target_assets)+2}. 外汇分析
{fx_analysis}

---

## {len(target_assets)+3}. 摩擦成本
```
{friction_report}
```

---

## {len(target_assets)+4}. 首席顾问决策 (DeepSeek)
{final_decision_ds}

---

## {len(target_assets)+5}. 首席顾问决策 (Gemini 第二意见)
{final_decision_gemini}
"""

    # --- 12. Append 到 memory/daily/<date>.md（供 Dreaming 用） ---
    daily_block = f"""**资产分析摘要**

- 宏观: {macro_analysis[:300]}...
- 外汇: {fx_analysis[:200]}...

**每个资产**:
"""
    for a in target_assets:
        sym = a["symbol"]
        daily_block += f"\n### {a.get('display_name', sym)} ({sym})\n{asset_analyses[sym][:400]}...\n"
    daily_block += f"\n**最终决策 (DeepSeek)**:\n{final_decision_ds[:600]}...\n"

    store.append_daily("daily_report", daily_block, date=today)

    # 同时记录原始数据快照（dreaming 反推用）
    snap_block = (
        f"**Market Data Snapshot**\n```\n"
        f"{macro_data_report}\n\n"
        f"{gold_report}\n\n"
        f"{friction_report}\n```\n"
    )
    store.append_daily("market_snapshot", snap_block, date=today)

    # --- 13. 发邮件 ---
    send_gmail_notification(full_report)

    return {
        "status": "success",
        "date": today,
        "assets": [a["symbol"] for a in target_assets],
        "decision_excerpt": final_decision_ds[:200],
    }


if __name__ == "__main__":
    print(run())
