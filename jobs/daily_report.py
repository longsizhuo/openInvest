"""每日投资报告 - Investment Committee 模式（4 角色）

替代旧的 main.py + bull/bear/judge/manager 多步骤管线。

流程：
1. Macro Strategist 跑 1 次（跨资产共享）
2. 对每个资产跑 Quant + Risk Officer + CIO（4 角色，但 Macro 是外部传入）
3. 直接拼报告发邮件 — 不再有 manager 综合层（CIO 已经综合）

LLM 调用次数: 1 (macro) + 3 * N (asset committee)
对比旧版: 1 (macro) + 5 * N (debate) + 1 (manager) → 新版省 token
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv

from core.committee import run_committee, run_macro_view
from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from db.market_store import MarketStore
from services.notifier import EmailDeliveryError, send_gmail_notification
from utils.exchange_fee import (
    analyze_multi_timeframe,
    get_cost_report,
    get_history_data,
    get_macro_data,
)
from utils.gold_price import format_gold_report, get_gold_snapshot

load_dotenv()

# 数据陈旧阈值：DB 最新日期距今超过这个天数，仍然能跑但要在 LLM 上下文里
# 显式标注"数据陈旧 N 天"，让 LLM 不要在过期价上面编今天的策略。
STALE_THRESHOLD_DAYS = int(os.getenv("INVEST_PRICE_STALE_DAYS", "3"))

_MARKET_STORE = MarketStore()


def _get_last_close(
    symbol: str, label: str
) -> Tuple[Optional[float], Optional[int]]:
    """返回 (close_price, age_days)。

    age_days: 0=今天的价、N=N 天前的价、None=完全没数据。
    price=None 时调用方必须显式判空，绝不能用 0 兜底——0 进入估值算式
    会让 NDQ 总值变 0，Risk Officer 看到"集中度爆表"建议清仓，全是数据
    缺失导致的虚假信号。
    """
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        print(f"⚠️ {label} 数据缺失: {symbol}")
        return None, None

    price = float(df["Close"].iloc[-1])

    # 算 staleness：DB 最新日期 vs. 今天
    latest_date_str = _MARKET_STORE.get_latest_date(symbol)
    if latest_date_str:
        try:
            latest = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
            age_days = (datetime.now().date() - latest).days
        except Exception:
            age_days = None
    else:
        age_days = None
    return price, age_days


def _format_staleness(label: str, age_days: Optional[int]) -> str:
    """给 portfolio_summary 用的陈旧警告字符串，age_days >= 阈值才输出。
    LLM 看到这段会知道当前估值用的是 N 天前的价，不要假装是今天的市场。"""
    if age_days is None or age_days < STALE_THRESHOLD_DAYS:
        return ""
    return (
        f"\n⚠️ **{label} 价格数据陈旧 {age_days} 天** —— 今日 scraper / yfinance "
        f"未能更新行情，估值基于 {age_days} 天前的收盘价。请在结论里明确标注"
        f"\"基于陈旧数据\"，不要假设当前价仍接近此值。"
    )


def _gather_relevant_insights(store: MemoryStore, asset: Dict[str, Any]) -> str:
    insights_dir = store.root / "insights"
    if not insights_dir.exists():
        return ""
    sym = asset.get("symbol", "").lower().replace("=", "_")
    matches = []
    for f in sorted(insights_dir.glob("*.md")):
        if sym in f.stem.lower() or any(
            tok in f.stem.lower() for tok in ["gold", "ndq"] if tok in sym
        ):
            matches.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')[:600]}")
    return "\n\n".join(matches)


def _portfolio_summary(
    pm: PortfolioManager,
    total_assets_cny: float,
    current_ndq_aud: float,
    current_gold_cny_per_g: float,
) -> str:
    """详细的用户上下文，给 Risk Officer 压力测试用 (含当前市价 + 浮盈)"""
    cash_cny = float(pm.portfolio.get("cash_cny", 0))
    aud_cash = float(pm.portfolio.get("aud_cash", 0))
    ndq_shares = float(pm.portfolio.get("ndq_shares", 0))
    ndq_cost = float(pm.portfolio.get("ndq_avg_cost_aud_per_share", 0))
    gold_grams = float(pm.portfolio.get("gold_grams", 0))
    gold_cost = float(pm.portfolio.get("gold_avg_cost_cny_per_gram", 0))
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0))
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
    dry_powder = max(0.0, cash_cny - buffer_cny)

    ndq_pnl_pct = ((current_ndq_aud / ndq_cost) - 1) * 100 if ndq_cost > 0 else 0
    gold_pnl_pct = (
        ((current_gold_cny_per_g / gold_cost) - 1) * 100 if gold_cost > 0 else 0
    )
    ndq_pnl_aud = (current_ndq_aud - ndq_cost) * ndq_shares if ndq_cost > 0 else 0
    gold_pnl_cny = (
        (current_gold_cny_per_g - gold_cost) * gold_grams if gold_cost > 0 else 0
    )

    return (
        f"用户风险偏好: {risk_level}\n"
        f"总资产估算: ¥{total_assets_cny:,.0f}\n"
        f"  - CNY 现金: ¥{cash_cny:,.0f} (其中应急金 ¥{buffer_cny:,} 不可投)\n"
        f"  - 可投子弹 (dry_powder): ¥{dry_powder:,.0f}\n"
        f"  - AUD 现金: ${aud_cash:,.0f}\n"
        f"  - **NDQ.AX**: {ndq_shares} 股, 均价 ${ndq_cost:.4f}, 现价 ${current_ndq_aud:.2f}, "
        f"浮盈 {ndq_pnl_pct:+.2f}% (≈ ${ndq_pnl_aud:+.2f} AUD)\n"
        f"  - **黄金 (浙商)**: {gold_grams:.4f}g, 均价 ¥{gold_cost:.2f}/g, "
        f"现价 ¥{current_gold_cny_per_g:.2f}/g, 浮盈 {gold_pnl_pct:+.2f}% "
        f"(≈ ¥{gold_pnl_cny:+,.2f})\n"
    )


def _run_gemini_cli_review(prompt: str) -> str:
    print("🤖 [Gemini CLI] 正在生成第二意见...")
    # PATH 上找 gemini，避免硬编码 nvm 路径（每升级 node 版本就失效）
    import shutil
    gemini_cmd = shutil.which("gemini")
    if not gemini_cmd:
        return "Skipped: gemini CLI 不在 PATH"
    try:
        result = subprocess.run(
            [gemini_cmd], input=prompt,
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "Skipped: gemini CLI 不可用"
    except Exception as e:
        return f"Skipped: {e}"


# ----------------------------------------------------------------------

def run() -> Dict[str, Any]:
    pm = PortfolioManager()
    store = pm.store
    today = datetime.now().strftime("%Y-%m-%d")

    target_assets = list(pm.strategy.get("target_assets", []))
    if not target_assets:
        return {"status": "skipped", "reason": "no_target_assets"}

    # 估值用的"主资产价格"专门给 NDQ 持仓估值用，不能依赖 target_assets 顺序。
    # 价格 None = 完全没数据（DB + scraper + yfinance 全失败），上层显式跳过该
    # 资产委员会而不是用 0 兜底；age_days = 数据陈旧多少天，超阈值要在 LLM 上下文
    # 里告警。
    data_warnings: list[str] = []   # 累积价格陈旧/缺失的警告，注入 portfolio_summary
    skipped_assets: set[str] = set()  # 完全没价的资产 → 跳过该资产 committee

    ndq_entry = next((a for a in target_assets if a.get("symbol") == "NDQ.AX"), None)
    if ndq_entry:
        ndq_price, ndq_age = _get_last_close("NDQ.AX", "NDQ.AX")
        if ndq_price is None:
            print("⛔ NDQ.AX 价格获取完全失败（scrape + yfinance + DB + CSV 均空），跳过 NDQ committee")
            store.dream_event({"phase": "price_fetch_failed", "symbol": "NDQ.AX", "date": today})
            skipped_assets.add("NDQ.AX")
            current_price = 0.0  # 仅给后面 cash_aud * rate 用，但 NDQ 持仓估值会被跳过
        else:
            current_price = ndq_price
            stale_msg = _format_staleness("NDQ.AX", ndq_age)
            if stale_msg:
                data_warnings.append(stale_msg)
                store.dream_event({"phase": "price_stale", "symbol": "NDQ.AX",
                                   "age_days": ndq_age, "date": today})
    else:
        current_price = 0.0  # 纯 CNY 组合，NDQ 持仓本来就 0

    rate_price, rate_age = _get_last_close("AUDCNY=X", "汇率")
    if rate_price is None:
        # 汇率拿不到比较罕见但仍要兜底——AUDCNY 的历史均值约 4.7 当作 sentinel
        # 避免直接抛异常让 daily_report 整体挂掉，但要明确告警 LLM
        print("⚠️ AUDCNY=X 完全失败，使用历史均值 4.7 兜底")
        store.dream_event({"phase": "price_fetch_failed", "symbol": "AUDCNY=X", "date": today})
        current_rate = 4.7
        data_warnings.append(
            "\n⚠️ **AUDCNY 汇率今日无法获取，使用历史均值 4.7 兜底**。汇率敏感的 AUD 估值"
            "可能偏差 ±5%，请勿据此做换汇决策。"
        )
    else:
        current_rate = rate_price
        stale_msg = _format_staleness("AUDCNY=X 汇率", rate_age)
        if stale_msg:
            data_warnings.append(stale_msg)
            store.dream_event({"phase": "price_stale", "symbol": "AUDCNY=X",
                               "age_days": rate_age, "date": today})

    # 计算总资产估算（给 Risk Officer 用）—— NDQ 跳过时不算它的市值
    user_status = pm.get_user_status(current_price, current_rate)
    # 从 strategy.target_assets[gold] 拿 price_offset_pct，让估值与用户成本同口径
    # （audit financial C1: 之前 offset_pct=0.0 + spot_cny_per_gram 让浮盈系统性
    # 偏低 1-1.5%）
    gold_offset = 0.0
    for a in target_assets:
        if a.get("symbol") == "GC=F":
            gold_offset = float(a.get("price_offset_pct", 0.0) or 0.0)
            break
    snap = get_gold_snapshot(offset_pct=gold_offset)
    if snap is None:
        store.dream_event({"phase": "price_fetch_failed", "symbol": "GC=F", "date": today})
        # 黄金 yfinance 没有 cache 兜底，失败就只能跳过黄金 committee
        skipped_assets.add("GC=F")
        gold_now = 0.0
        data_warnings.append(
            "\n⚠️ **黄金现货今日无法获取**（GC=F + USDCNY 双双失败），"
            "本次跳过黄金 committee 分析。"
        )
    else:
        gold_now = snap.bank_cny_per_gram  # 含浙商点差的克价，与用户成本同口径

    gold_grams = float(pm.portfolio.get("gold_grams", 0))
    ndq_shares = float(pm.portfolio.get("ndq_shares", 0))
    # 跳过的资产从总资产估算里剔除，避免用 0 当价格污染集中度计算
    ndq_value_cny = ndq_shares * current_price * current_rate if "NDQ.AX" not in skipped_assets else 0.0
    gold_value_cny = gold_grams * gold_now if "GC=F" not in skipped_assets else 0.0
    total_assets_cny = (
        user_status.cash_cny
        + user_status.cash_aud * current_rate
        + ndq_value_cny
        + gold_value_cny
    )
    portfolio_summary = _portfolio_summary(
        pm, total_assets_cny,
        current_ndq_aud=current_price,
        current_gold_cny_per_g=gold_now,
    )
    if data_warnings:
        portfolio_summary += "\n\n=== 数据可信度告警 ===" + "".join(data_warnings)

    has_non_cny = any(a.get("currency", "CNY") != "CNY" for a in target_assets)

    # 摩擦成本（NDQ 才有，黄金没有）
    if has_non_cny:
        friction_report = get_cost_report(
            invest_cny=user_status.disposable_for_invest,
            spot_rate=current_rate,
        )
    else:
        friction_report = "N/A (本期无需换汇)"

    # 1) Macro Strategist 跑一次（跨资产共享）
    print("🌍 Macro Strategist (1 次)...")
    macro_data_report = get_macro_data()
    macro_view = run_macro_view(macro_data_report)
    print(f"  Macro: {macro_view[:120]}")

    # 2) 对每个资产跑 committee（数据完全缺失的资产直接跳过，不让 LLM 在 0 价上瞎编）
    asset_committees: Dict[str, Dict[str, Any]] = {}
    for asset in target_assets:
        sym = asset["symbol"]
        if sym in skipped_assets:
            print(f"⏭️  Skip committee for {sym}（价格数据缺失）")
            continue
        print(f"\n⚖️ Committee for {sym}...")
        market_data = analyze_multi_timeframe(
            get_history_data(sym, "2y"),
            f"{asset.get('display_name', sym)} ({sym})",
        )
        prior = _gather_relevant_insights(store, asset)
        result = run_committee(
            asset=asset,
            market_data=market_data,
            macro_view=macro_view,
            portfolio_summary=portfolio_summary,
            prior_insights=prior,
        )
        asset_committees[sym] = result
        v = result["verdict"]
        print(
            f"  ⚖️  {sym}: {v['verdict']} "
            f"(conf {v['confidence']:.2f}, dom {v['dominant_view']}, "
            f"alloc ¥{v['alloc_cny']})"
        )

    # 3) Gemini 第二意见（综合所有资产 verdicts）
    cio_memos_combined = "\n\n".join([
        f"### {a.get('display_name', a['symbol'])} ({a['symbol']})\n"
        f"{asset_committees[a['symbol']]['report'].cio_memo}"
        for a in target_assets
    ])
    gold_snapshot_text = format_gold_report(snap) if snap else "黄金数据获取失败"
    gemini_prompt = f"""
今日 Investment Committee 给出以下决策（每个资产 4 角色 + CIO 综合）：

# 用户上下文
{portfolio_summary}

# 宏观环境
{macro_view}

# 各资产 CIO 备忘
{cio_memos_combined}

# 黄金现货
{gold_snapshot_text}

# 摩擦成本
{friction_report}

请用搜索工具验证最新汇率/价格，对委员会的决策做独立 challenge。
**必须中文回答，控制在 300 字以内**。给一个总结性的"我同意 / 我反对"判断。
"""
    final_decision_gemini = _run_gemini_cli_review(gemini_prompt)

    # 4) 拼报告
    asset_section = "\n\n---\n\n".join([
        f"## {idx+2}. {a.get('display_name', a['symbol'])} ({a['symbol']})\n\n"
        f"**裁决**: {asset_committees[a['symbol']]['verdict']['verdict']} | "
        f"置信度 {asset_committees[a['symbol']]['verdict']['confidence']:.2f} | "
        f"主导方 {asset_committees[a['symbol']]['verdict']['dominant_view']} | "
        f"建议金额 ¥{asset_committees[a['symbol']]['verdict']['alloc_cny']}\n\n"
        f"### CIO 备忘\n```\n{asset_committees[a['symbol']]['report'].cio_memo}\n```\n\n"
        f"<details><summary>📜 三个 analyst 详细意见</summary>\n\n"
        f"**Quant**:\n{asset_committees[a['symbol']]['report'].quant_view}\n\n"
        f"**Risk Officer**:\n{asset_committees[a['symbol']]['report'].risk_view}\n\n"
        f"</details>"
        for idx, a in enumerate(target_assets)
    ])

    full_report = f"""
# 投资委员会日报 ({today})

## 1. 宏观环境 (跨资产共享)
{macro_view}

---

## 黄金现货快照
```
{gold_snapshot_text}
```

---

{asset_section}

---

## {len(target_assets)+2}. 摩擦成本 (CNY → AUD 换汇)
```
{friction_report}
```

---

## {len(target_assets)+3}. Gemini 第二意见 (独立 challenge)
{final_decision_gemini}

---

*用户当前总资产估算: ¥{total_assets_cny:,.0f}*
*Generated by Investment Committee — Quant / Macro / Risk Officer / CIO*

---

### ⚠️ 风险提示与免责声明

- 本报告由 LLM 生成，**不构成任何投资建议**。LLM 可能误读数据、过度自信、漏看
  重要信息或基于陈旧/错误数据编造结论。
- 系统**不自动下单**，所有决策需人工复核后自行执行。
- 数据样本量过小（近 60 天），任何"跑赢/跑输基准"的结论在统计意义上**不显著**，
  不代表长期表现。
- 投资有风险，过往业绩不预示未来。损失自负。
"""

    # 5) Append 给 Dreaming（被跳过的资产标 N/A）
    daily_block = f"**委员会摘要**\n\n- 宏观: {macro_view[:200]}\n\n**资产裁决**:"
    for a in target_assets:
        sym = a["symbol"]
        if sym in skipped_assets:
            daily_block += f"\n- {a.get('display_name', sym)} ({sym}): SKIPPED（数据缺失）"
            continue
        v = asset_committees[sym]["verdict"]
        daily_block += (
            f"\n- {a.get('display_name', sym)} ({sym}): "
            f"{v['verdict']} (conf {v['confidence']:.2f}, alloc ¥{v['alloc_cny']})"
        )
    store.append_daily("committee_report", daily_block, date=today)
    store.append_daily(
        "market_snapshot",
        f"```\n{macro_data_report}\n\n{gold_snapshot_text}\n\n{friction_report}\n```",
        date=today,
    )

    # 6) 发邮件
    # committee 结果已经持久化到 .committee/<date>/ 和 daily/<date>.md，邮件
    # 失败不应该让整个 job 状态变成 failed —— 但必须能在 return value 和审计日志
    # 里看到 email 失败这件事，让外部监控（看 dream_event）能告警。
    email_status: Dict[str, Any] = {"sent": False, "receiver": "", "error": None}
    try:
        receiver = send_gmail_notification(full_report)
        email_status = {
            "sent": bool(receiver),
            "receiver": receiver,
            "error": None,
            "skipped": not receiver,  # 凭据缺失等于故意 skip
        }
    except EmailDeliveryError as e:
        email_status = {"sent": False, "receiver": "", "error": str(e), "skipped": False}
        print(f"⛔ Email delivery failed (committee 已落盘，job 仍标 success): {e}")
        store.dream_event({
            "phase": "email_delivery_failed",
            "date": today,
            "error": str(e),
        })

    return {
        "status": "success" if not skipped_assets else "degraded",
        "date": today,
        "assets": [a["symbol"] for a in target_assets],
        "skipped_assets": sorted(skipped_assets),
        "data_warnings": data_warnings,
        "email": email_status,
        "verdicts": {
            sym: {
                "verdict": r["verdict"]["verdict"],
                "confidence": r["verdict"]["confidence"],
                "alloc_cny": r["verdict"]["alloc_cny"],
            }
            for sym, r in asset_committees.items()
        },
    }


if __name__ == "__main__":
    print(run())
