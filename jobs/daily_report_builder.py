"""每日投资报告"构建器" - 纯函数，零 IO 副作用

ADR-005：从 daily_report.py 拆分出"给定数据 → 组装 markdown"的纯函数层，
让调度/采集与报告渲染解耦，并且让单测不需要 mock yfinance/LLM 就能跑。

职责（本文件）：
- portfolio_summary_text()：给定 pm + 价格 + 总资产，生成 Risk Officer 上下文文本
- format_staleness_warning()：给定 label + age_days，生成告警字符串
- assemble_full_report()：给定所有委员会结果 + 辅助数据，组装最终 markdown 报告
- classify_asset_freshness()：stateless 辅助函数（age_days → fresh/stale/very_stale）

不在本文件里的：
- 价格拉取（get_history_data / get_gold_snapshot）
- LLM 调用（run_committee / run_macro_view）
- 邮件发送（send_gmail_notification）
- cron 触发 / 熔断判断（日期/阈值/跳过逻辑仍在 daily_report.py）
- MemoryStore / dream_event 写入（IO 副作用）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.portfolio_manager import PortfolioManager

# ============ 常量（可被 daily_report.py 的 env 读取覆盖后传入，无默认值依赖） ============

STALE_LABEL_SOFT = "stale"       # 软警告（超阈值但在熔断阈值内）
STALE_LABEL_HARD = "very_stale"  # 硬熔断
STALE_LABEL_MISSING = "missing"  # 完全没价
STALE_LABEL_FRESH = "fresh"      # 新鲜


# ============ 1. 辅助：Staleness 分类 ============

def classify_asset_freshness(
    price: Optional[float],
    age_days: Optional[int],
    stale_threshold_days: int = 3,
    hard_abort_days: int = 7,
) -> str:
    """把 (price, age_days) 映射到 freshness 标签（stateless，易于单测）

    Args:
        price: 当前价，None 表示完全没拿到
        age_days: 数据距今天数，None 表示不知道（按 fresh 处理）
        stale_threshold_days: 超过几天认为"有点旧"（软警告）
        hard_abort_days: 超过几天认为"太旧，不可信"（硬熔断）
    """
    if price is None:
        return STALE_LABEL_MISSING
    if age_days is None or age_days < stale_threshold_days:
        return STALE_LABEL_FRESH
    if age_days < hard_abort_days:
        return STALE_LABEL_SOFT
    return STALE_LABEL_HARD


# ============ 2. 辅助：告警文本 ============

def format_staleness_warning(label: str, age_days: Optional[int], stale_threshold_days: int = 3) -> str:
    """给 portfolio_summary 用的陈旧警告字符串，age_days >= 阈值才输出。

    LLM 看到这段会知道当前估值用的是 N 天前的价，不要假装是今天的市场。
    返回空字符串表示无需警告。
    """
    if age_days is None or age_days < stale_threshold_days:
        return ""
    return (
        f"\n⚠️ **{label} 价格数据陈旧 {age_days} 天** —— 今日 scraper / yfinance "
        f"未能更新行情，估值基于 {age_days} 天前的收盘价。请在结论里明确标注"
        f"\"基于陈旧数据\"，不要假设当前价仍接近此值。"
    )


# ============ 3. 核心：portfolio summary 文本生成 ============

def portfolio_summary_text(
    pm: PortfolioManager,
    total_assets_cny: float,
    current_prices: Dict[str, float],
) -> str:
    """详细的用户上下文，给 Risk Officer 压力测试用（含当前市价 + 浮盈）

    v3 通用化：动态遍历用户实际 holdings，不再写死 NDQ.AX/GC=F。fork 用户
    持仓 510300.SS / AAPL / BTC-USD 等任何 yfinance symbol 都能正确显示。

    Args:
        pm: PortfolioManager 实例（只读，不触发任何写）
        total_assets_cny: 已经计算好的总资产（CNY 折算）
        current_prices: {symbol: 当前价} dict（per asset 币种）

    Returns:
        多行字符串，结尾带 \\n
    """
    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0))
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
    dry_powder = max(0.0, cash_cny - buffer_cny)

    # 现金部分（多币种通用）
    lines = [
        f"用户风险偏好: {risk_level}",
        f"总资产估算: ¥{total_assets_cny:,.0f}",
        f"  - CNY 现金: ¥{cash_cny:,.0f} (其中应急金 ¥{buffer_cny:,} 不可投)",
        f"  - 可投子弹 (dry_powder): ¥{dry_powder:,.0f}",
    ]
    if aud_cash > 0:
        lines.append(f"  - AUD 现金: ${aud_cash:,.0f}")

    # 持仓部分：遍历实际 holdings，按 unit_label / cost_currency 通用化展示
    real_holdings = [
        h for h in pm.holdings
        if not h.get("is_tracking_only") and float(h.get("units", 0) or 0) > 0
    ]
    if not real_holdings:
        lines.append("  - **当前无实仓持仓**（onboarding 后请通过 GUI/NapCat 添加）")

    for h in real_holdings:
        sym = str(h.get("symbol", ""))
        units = float(h.get("units", 0) or 0)
        cost = float(h.get("avg_cost", 0) or 0)
        unit_label = str(h.get("unit_label", "份"))
        ccy = str(h.get("cost_currency", "CNY"))
        display = h.get("display_name") or sym
        channel = h.get("channel") or ""
        channel_str = f" ({channel})" if channel else ""

        cur = current_prices.get(sym)
        if cur is None or cost <= 0:
            # 缺价 / 无成本时仅显示持仓量
            lines.append(
                f"  - **{display}** ({sym}){channel_str}: "
                f"{units:.4f} {unit_label}, 均价 {cost:.2f} {ccy}/{unit_label}",
            )
            continue

        pnl_pct = ((cur / cost) - 1) * 100
        pnl_local = (cur - cost) * units
        ccy_symbol = "¥" if ccy == "CNY" else ("$" if ccy in ("USD", "AUD") else "")
        lines.append(
            f"  - **{display}** ({sym}){channel_str}: "
            f"{units:.4f} {unit_label}, "
            f"均价 {ccy_symbol}{cost:.2f}, "
            f"现价 {ccy_symbol}{cur:.2f}, "
            f"浮盈 {pnl_pct:+.2f}% (≈ {ccy_symbol}{pnl_local:+,.2f} {ccy})",
        )

    return "\n".join(lines) + "\n"


# ============ 4. 核心：完整报告拼接 ============

def assemble_full_report(
    today: str,
    macro_view: str,
    gold_snapshot_text: str,
    friction_report: str,
    target_assets: List[Dict[str, Any]],
    asset_committees: Dict[str, Dict[str, Any]],
    skipped_assets: set,
    total_assets_cny: float,
    final_decision_gemini: str,
) -> str:
    """给定所有委员会结果 + 辅助数据，组装最终 markdown 报告

    这是纯函数：输入什么就输出什么，没有任何 IO 调用。
    测试时直接传 fixture 数据即可，不需要 mock LLM / yfinance。

    Args:
        today: 日期字符串（YYYY-MM-DD）
        macro_view: Macro Strategist 的宏观分析文本
        gold_snapshot_text: 黄金现货快照文本
        friction_report: 摩擦成本报告文本
        target_assets: strategy.target_assets 列表
        asset_committees: {symbol: {"verdict": ..., "report": ...}} 各资产委员会结果
        skipped_assets: 被跳过的资产 symbol 集合（数据缺失）
        total_assets_cny: 总资产 CNY 估算（已计算好）
        final_decision_gemini: Gemini 第二意见文本

    Returns:
        完整 markdown 报告字符串
    """
    # 各资产委员会区块
    active_assets = [a for a in target_assets if a["symbol"] not in skipped_assets
                     and a["symbol"] in asset_committees]

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
        for idx, a in enumerate(active_assets)
    ]) if active_assets else "_（所有资产数据不可用，跳过委员会）_"

    n = len(active_assets)  # 活跃资产数，用于后续章节编号

    return f"""
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

## {n+2}. 摩擦成本 (CNY → AUD 换汇)
```
{friction_report}
```

---

## {n+3}. Gemini 第二意见 (独立 challenge)
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
