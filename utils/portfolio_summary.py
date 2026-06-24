"""Portfolio summary 文本生成 helper（给 Risk Officer 看的用户上下文）

历史背景（2026-05-19）：
- 原本 portfolio_summary_text 写在 jobs/daily_report_builder.py 里。
- cron 路径（daily_report.py）和 skill prepare 路径（scripts/skill.py:cmd_prepare_committee）
  都从 jobs/ 导出这个函数；但 Direct 路径（scripts/skill.py:cmd_run_committee →
  core/committee_runner.py:run_committee_session → run_committee_for_symbol）
  用的是 service layer 自己拼的简化版（只 4 行：风险偏好 / CNY 现金 / AUD 现金 /
  目标资产单位），**没有总资产、没有所有持仓、没有集中度数字**。
- 结果 LLM (Risk Officer) 自己算集中度连续 6 天算错（真实 33.4% 算成 81.6%），
  推荐"建议减仓"。
- 分层契约（CLAUDE.md）禁止 core/ 反向 import jobs/ → 不能让
  core.committee_runner 调 jobs.daily_report_builder.portfolio_summary_text。
- 最干净的做法：把 portfolio_summary_text 搬到 utils/，让所有 entry 和 service layer
  都能用。portfolio summary 本质是"展示 helper"不是 daily_report 业务逻辑。

向后兼容：jobs/daily_report_builder.py 仍然 re-export 这个函数，外部
`from jobs.daily_report_builder import portfolio_summary_text` 不会破。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from core.portfolio_manager import PortfolioManager


def portfolio_summary_text(
    pm: "PortfolioManager",
    total_assets_cny: float,
    current_prices: Dict[str, float],
    *,
    backup_cny: float = 0.0,
) -> str:
    """详细的用户上下文，给 Risk Officer 压力测试用（含当前市价 + 浮盈 + 集中度）

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

    # 总资产是否可用：NaN / 非有限（上游某腿不可解析污染了 total）时显式降级，
    # 绝不渲染 ¥nan 或据假值算集中度。
    total_ok = (
        isinstance(total_assets_cny, (int, float))
        and math.isfinite(total_assets_cny)
        and total_assets_cny > 0
    )
    total_str = f"¥{total_assets_cny:,.0f}" if total_ok else "¥不可用"

    # 现金部分（多币种通用）
    lines = [
        f"用户风险偏好: {risk_level}",
        f"总资产估算: {total_str}",
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

    # 算每个 holding 的 market value (CNY) 用于集中度
    # 2026-05-19 修复：LLM 自己算集中度连续 6 天错算 68.5%（真实 33.3%），
    # 直接显式输出每个 asset 的 concentration_pct 给 Risk Officer 用。
    # 用 utils.fx.to_base 而非硬编码 if ccy=="AUD"，支持任意币种 (EUR/JPY/HKD 等)。
    from utils.fx import to_base
    holding_values_cny: Dict[str, float] = {}
    for h in real_holdings:
        sym = str(h.get("symbol", ""))
        units = float(h.get("units", 0) or 0)
        ccy = str(h.get("cost_currency", "CNY"))
        cur = current_prices.get(sym)
        # NaN 价等同缺价：不写进 holding_values_cny（否则 NaN 市值会污染下游展示）
        if cur is None or not math.isfinite(cur):
            continue
        local_value = units * cur
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
        value_cny = to_base(ccy, local_value, "CNY")
        if value_cny is not None and math.isfinite(value_cny):
            holding_values_cny[sym] = value_cny

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
        # NaN 价等同缺价：只显示持仓量 + 均价，不算浮盈/集中度
        if cur is None or not math.isfinite(cur) or cost <= 0:
            # 缺价 / 无成本时仅显示持仓量
            lines.append(
                f"  - **{display}** ({sym}){channel_str}: "
                f"{units:.4f} {unit_label}, 均价 {cost:.2f} {ccy}/{unit_label}",
            )
            continue

        pnl_pct = ((cur / cost) - 1) * 100
        pnl_local = (cur - cost) * units
        ccy_symbol = "¥" if ccy == "CNY" else ("$" if ccy in ("USD", "AUD") else "")
        # 集中度 = 该 asset CNY 市值 / total_assets_cny
        value_cny = holding_values_cny.get(sym, 0.0)
        if total_ok:
            conc_pct = value_cny / total_assets_cny * 100
            conc_str = (
                f"**集中度 {conc_pct:.1f}%** "
                f"(CNY 市值 ¥{value_cny:,.0f} / 总资产 ¥{total_assets_cny:,.0f})"
            )
        else:
            # total 不可用（NaN/缺）：绝不伪造 0.0%，输出可见降级标记促人工复核
            conc_str = (
                f"**集中度 暂不可计算**（总资产不可用，请勿据此做集中度判断；"
                f"CNY 市值 ¥{value_cny:,.0f}）"
            )
        lines.append(
            f"  - **{display}** ({sym}){channel_str}: "
            f"{units:.4f} {unit_label}, "
            f"均价 {ccy_symbol}{cost:.2f}, "
            f"现价 {ccy_symbol}{cur:.2f}, "
            f"浮盈 {pnl_pct:+.2f}% (≈ {ccy_symbol}{pnl_local:+,.2f} {ccy}), "
            f"{conc_str}",
        )

    # 真实总财富占比注释（当有兜底 backup 时附注，给 Risk Officer / CIO 参考）
    if backup_cny > 0:
        if total_ok:
            real_total = total_assets_cny + backup_cny
            account_ratio = (total_assets_cny / real_total * 100) if real_total > 0 else 0.0
            lines.append(
                f"  [兜底注释] 账户总资产 ¥{total_assets_cny:,.0f} 占真实总财富 "
                f"¥{real_total:,.0f} 的 {account_ratio:.1f}%，"
                f"BACKUP ¥{backup_cny:,.0f} 仅作风险兜底不可投资。"
                f"账户归零不影响生存。"
            )
        else:
            # 总资产不可用：仍附注 BACKUP 存在，但占比暂不可算（绝不渲染 ¥nan）
            lines.append(
                f"  [兜底注释] 账户总资产暂不可用（请勿据此判断），"
                f"BACKUP ¥{backup_cny:,.0f} 仅作风险兜底不可投资。"
                f"账户归零不影响生存。"
            )

    return "\n".join(lines) + "\n"
