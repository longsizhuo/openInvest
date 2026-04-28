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
from typing import Any, Dict

from dotenv import load_dotenv

from core.committee import run_committee, run_macro_view
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


def _get_last_close(symbol: str, label: str) -> float:
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        print(f"⚠️ {label} 数据缺失: {symbol}")
        return 0.0
    return float(df["Close"].iloc[-1])


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
    gemini_cmd = "/home/ubuntu/.nvm/versions/node/v24.13.0/bin/gemini"
    if not os.path.exists(gemini_cmd):
        gemini_cmd = "gemini"
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

    primary_symbol = target_assets[0]["symbol"]
    current_price = _get_last_close(primary_symbol, "主资产")
    current_rate = _get_last_close("AUDCNY=X", "汇率")

    # 计算总资产估算（给 Risk Officer 用）
    user_status = pm.get_user_status(current_price, current_rate)
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_now = snap.spot_cny_per_gram if snap else 0.0
    gold_grams = float(pm.portfolio.get("gold_grams", 0))
    total_assets_cny = (
        user_status.cash_cny
        + user_status.cash_aud * current_rate
        + float(pm.portfolio.get("ndq_shares", 0)) * current_price * current_rate
        + gold_grams * gold_now
    )
    portfolio_summary = _portfolio_summary(
        pm, total_assets_cny,
        current_ndq_aud=current_price,
        current_gold_cny_per_g=gold_now,
    )

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

    # 2) 对每个资产跑 committee
    asset_committees: Dict[str, Dict[str, Any]] = {}
    for asset in target_assets:
        sym = asset["symbol"]
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
"""

    # 5) Append 给 Dreaming
    daily_block = f"**委员会摘要**\n\n- 宏观: {macro_view[:200]}\n\n**资产裁决**:"
    for a in target_assets:
        sym = a["symbol"]
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
    send_gmail_notification(full_report)

    return {
        "status": "success",
        "date": today,
        "assets": [a["symbol"] for a in target_assets],
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
