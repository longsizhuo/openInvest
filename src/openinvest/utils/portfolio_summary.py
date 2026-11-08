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
import json
from typing import TYPE_CHECKING, Any, Dict, Sequence

if TYPE_CHECKING:
    from openinvest.core.portfolio_manager import PortfolioManager


_READABLE_LEDGER_STATUSES = frozenset({"client_recorded", "simulation_only", "requires_reconciliation"})
_QUALIFIED_BALANCE_BASES = frozenset({"client_recorded", "client_observed_balance", "broker_recorded",
                                    "paper_simulated", "broker_paper_recorded"})


def source_status_readable(evidence: Dict[str, Any]) -> bool:
    return evidence.get("status") in _READABLE_LEDGER_STATUSES


def holding_source_grade(evidence: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    """Missing provenance never defaults to a reported client balance."""
    grade = dict((evidence.get("holdings") or {}).get(symbol) or {})
    for field in ("current_quantity", "current_value", "cost_basis"):
        grade[field] = (grade.get(field) or "unknown") if source_status_readable(evidence) else "unknown"
    return grade


def balance_field_qualified(grade: Dict[str, Any], field: str) -> bool:
    return grade.get(field) in _QUALIFIED_BALANCE_BASES


def portfolio_summary_text(
    pm: "PortfolioManager",
    total_assets: float,
    current_prices: Dict[str, float],
    excluded: Sequence[str] = (),
) -> str:
    """详细的用户上下文，给 Risk Officer 压力测试用（含当前市价 + 浮盈 + 集中度）

    v3 通用化：动态遍历用户实际 holdings，不再写死 NDQ.AX/GC=F。fork 用户
    持仓 510300.SS / AAPL / BTC-USD 等任何 yfinance symbol 都能正确显示。

    Args:
        pm: PortfolioManager 实例（只读，不触发任何写）
        total_assets: 已经计算好的总资产（按基准币种 utils.base_currency 折算，调用方用同一基准算）
        current_prices: {symbol: 当前价} dict（per asset 币种）
        excluded: 没计入总资产的条目（fx.unvalued：缺汇率 / 缺价），总资产旁明说

    Returns:
        多行字符串，结尾带 \\n
    """
    from openinvest.utils.base_currency import base_currency, money
    base = base_currency()
    from openinvest.services.ledger_evidence import account_source_status
    ledger_evidence=account_source_status(pm)
    source_available = source_status_readable(ledger_evidence)
    cash_qualified = source_available and ledger_evidence.get("allocation_cash_eligible", True)
    paper = ledger_evidence.get("ledger_domain") in {"paper_simulation", "broker_paper"}
    def has_market_price(symbol):
        price = current_prices.get(symbol)
        return isinstance(price, (int, float)) and math.isfinite(price) and price > 0
    valuation_qualified = all(
        balance_field_qualified(holding_source_grade(ledger_evidence, str(h.get("symbol") or "")), "current_quantity")
        and (has_market_price(str(h.get("symbol") or ""))
             or balance_field_qualified(holding_source_grade(ledger_evidence, str(h.get("symbol") or "")), "cost_basis"))
        for h in pm.holdings if not h.get("is_tracking_only"))
    cash_base = pm.cash_amount(base)
    aud_cash = pm.cash_amount("AUD")
    risk_level = str(pm.user.get("risk_tolerance") or "未提供")
    # 可投子弹 = 所有币种现金折算成基准币种（方案 A，2026-10）；只有基准币种现金时与旧口径相同
    from openinvest.utils.fx import dry_powder as _dry_powder
    dry_powder, dp_missing = _dry_powder(pm, base) if cash_qualified else (None, [])
    foreign_cash = {str(c).upper(): float(a or 0) for c, a in pm.cash.items()
                    if str(c).upper() != base and float(a or 0) > 0}

    # 总资产是否可用：NaN / 非有限（上游某腿不可解析污染了 total）时显式降级，
    # 绝不渲染 ¥nan 或据假值算集中度。
    total_ok = (
        cash_qualified and valuation_qualified and isinstance(total_assets, (int, float))
        and math.isfinite(total_assets)
        and total_assets > 0
    )
    total_str = money(total_assets, base) if total_ok else ("¥不可用" if base == "CNY" else "不可用")
    if not source_available:
        total_str = "未知（来源资格暂无法核对）"
    elif not valuation_qualified and isinstance(total_assets, (int, float)) and math.isfinite(total_assets):
        total_str = "未核跟踪投影 " + money(total_assets, base) + "（非已确认账户余额）"
    if excluded and source_available:
        total_str += f"（未计入 {'、'.join(excluded)}：拉不到汇率或价格，实际总资产更高）"

    # 现金部分（多币种通用）
    lines = [
        f"用户风险偏好: {risk_level}",
        f"总资产估算: {total_str}",
        f"  - {base} 现金: " + (money(cash_base, base) if cash_qualified else "未知（来源或费用待核）"),
        f"  - 可投子弹 (dry_powder): " + (money(dry_powder, base) if cash_qualified else "未知（来源或费用待核）")
        + ("（含外币现金按汇率折算）" if cash_qualified and set(foreign_cash) - set(dp_missing) else "")
        + (f"（未计入 {'、'.join(dp_missing)} 现金：拉不到汇率）" if dp_missing else ""),
    ]
    if paper:
        lines.insert(0,'[PAPER ACCOUNT] 这里是纸面/模拟账户；金额不是用户真实资金能力，也不证明实盘采纳。')
    if ledger_evidence['status']=='requires_reconciliation':
        lines.append('[QUALITY_ALERT] 账本有独立列出的来源/用途缺口（数量、成本、账户域或费用等）；不能把未核字段当作实盘成交、可承受亏损或实际收益。')
    elif not source_available:
        lines.insert(0,'[QUALITY_ALERT] 账本来源资格暂无法核对；数量、成本、可用资金和盈亏均未知。')
    if cash_qualified and aud_cash > 0 and base != "AUD":
        lines.append(f"  - AUD 现金: ${aud_cash:,.0f}")
    for ccy, amt in sorted(foreign_cash.items()):
        if cash_qualified and ccy != "AUD":
            lines.append(f"  - {ccy} 现金: {amt:,.0f} {ccy}")

    # 在途场外基金申购（ADR-027）：现金已扣、份额还没到，总资产已按面值含它——明说，免得 Risk 看到
    # "总资产 > 现金 + 持仓"自己去对账，或把这只基金当成没仓位。没有 pending_fund_orders 的替身 = 没有在途
    pending_fn = getattr(pm, "pending_fund_orders", None)
    for o in (pending_fn() if source_available and callable(pending_fn) else []):
        ccy = str(o.get("currency") or "CNY").upper()
        amt = float(o.get("amount") or 0)
        lines.append(
            f"  - 在途申购 {o.get('symbol')}: {money(amt, base) if ccy == base else f'{amt:,.2f} {ccy}'}"
            f"（现金已扣、份额预计 {o.get('expected_confirm_date')} 确认，已按面值含在总资产里）")

    # 持仓部分：遍历实际 holdings，按 unit_label / cost_currency 通用化展示
    real_holdings = [
        h for h in pm.holdings
        if not h.get("is_tracking_only") and (float(h.get("units", 0) or 0) > 0
            or not balance_field_qualified(holding_source_grade(ledger_evidence, str(h.get("symbol") or "")), "current_quantity"))
    ]
    if not real_holdings:
        lines.append("  - **当前无实仓持仓**（onboarding 后请通过 GUI/NapCat 添加）" if source_available
                     else "  - 持仓来源资格未知，暂不能确认当前持仓。")

    # 算每个 holding 的 market value（基准币种）用于集中度
    # 2026-05-19 修复：LLM 自己算集中度连续 6 天错算 68.5%（真实 33.3%），
    # 直接显式输出每个 asset 的 concentration_pct 给 Risk Officer 用。
    # 用 utils.fx.to_base 而非硬编码 if ccy=="AUD"，支持任意币种 (EUR/JPY/HKD 等)。
    from openinvest.utils.fx import to_base
    from openinvest.core.config import load_config
    # 集中度 lens 关闭(ADR-020：单资产/刻意集中策略)→ 单一源在此 gate,不渲染集中度。
    # 这是 cron / session / Direct 三路径共用的 helper,在此关一处即全关;下游
    # _extract_concentration_from_summary 取不到 → cio_parse 覆写 no-op，Risk 也拿不到数字。
    _show_conc = bool(load_config().verdict.concentration_lens_enabled)
    holding_values: Dict[str, float] = {}
    for h in real_holdings:
        sym = str(h.get("symbol", ""))
        units = float(h.get("units", 0) or 0)
        ccy = str(h.get("cost_currency", "CNY"))
        cur = current_prices.get(sym)
        if not source_available:
            continue
        # NaN 价等同缺价：不写进 holding_values（否则 NaN 市值会污染下游展示）
        if cur is None or not math.isfinite(cur):
            continue
        local_value = units * cur
        # live valuation: as_of_date intentionally not threaded (no historical caller).
        # For backtest/historical use, thread as_of_date like utils.fx.to_base (see PR#53 fix(fx)).
        value = to_base(ccy, local_value, base)
        if value is not None and math.isfinite(value):
            holding_values[sym] = value

    for h in real_holdings:
        sym = str(h.get("symbol", ""))
        units = float(h.get("units", 0) or 0)
        cost = float(h.get("avg_cost", 0) or 0)
        unit_label = str(h.get("unit_label", "份"))
        ccy = str(h.get("cost_currency", "CNY"))
        display = h.get("display_name") or sym
        channel = h.get("channel") or ""
        channel_str = f" ({channel})" if channel else ""

        grade=holding_source_grade(ledger_evidence,sym)
        observations = {field: observation for field, observation in (grade.get("balance_observations") or {}).items()
                        if grade.get(field) == "client_observed_balance"}
        observation_lines = (["    已声明余额观察（保留原 as_of，不反推其它字段）：" + json.dumps(observations, ensure_ascii=False)]
                             if observations else [])
        if not balance_field_qualified(grade, "current_quantity"):
            value=holding_values.get(sym)
            lines.append(f'  - **{display}** ({sym}){channel_str}: 未核对的跟踪/价值代理；不提供真实数量、持仓总成本或浮盈。'
                         +(f'代理估值约 {money(value,base)}，不是已确认平台余额。' if value is not None else '代理估值暂不可用。'))
            lines.extend(observation_lines)
            continue
        if not balance_field_qualified(grade, "cost_basis"):
            reason = "手续费待核，成本未知" if grade.get("cost_basis") == "broker_commission_pending" else "成本来源待核"
            lines.append(f'  - **{display}** ({sym}){channel_str}: 已记录数量 {units:.4f} {unit_label}；{reason}，不报告浮盈。')
            lines.extend(observation_lines)
            continue

        cur = current_prices.get(sym)
        # NaN 价等同缺价：只显示持仓量 + 均价，不算浮盈/集中度
        if cur is None or not math.isfinite(cur) or cost <= 0:
            # 缺价 / 无成本时仅显示持仓量
            lines.append(
                f"  - **{display}** ({sym}){channel_str}: "
                f"{units:.4f} {unit_label}, 均价 {cost:.2f} {ccy}/{unit_label}",
            )
            lines.extend(observation_lines)
            continue

        pnl_pct = ((cur / cost) - 1) * 100
        pnl_local = (cur - cost) * units
        ccy_symbol = "¥" if ccy == "CNY" else ("$" if ccy in ("USD", "AUD") else "")
        line = (
            f"  - **{display}** ({sym}){channel_str}: "
            f"{units:.4f} {unit_label}, "
            f"均价 {ccy_symbol}{cost:.2f}, "
            f"现价 {ccy_symbol}{cur:.2f}, "
            f"{'模拟浮盈' if paper else '浮盈'} {pnl_pct:+.2f}% (≈ {ccy_symbol}{pnl_local:+,.2f} {ccy})"
        )
        # 该腿汇率拉不到时 value=None：市值 / 集中度 / 压力测试都标"暂不可计算"，不当 0 用
        value = holding_values.get(sym)
        # 集中度 = 该 asset 基准币种市值 / total_assets —— 仅 lens 开启时渲染（ADR-020）
        if _show_conc:
            if total_ok and value is not None:
                conc_pct = value / total_assets * 100
                line += (
                    f", **集中度 {conc_pct:.1f}%** "
                    f"({base} 市值 {money(value, base)} / 总资产 {money(total_assets, base)})"
                )
            else:
                # total 或该腿汇率不可用：绝不伪造 0.0%，输出可见降级标记促人工复核
                mv = f"；{base} 市值 {money(value, base)}" if value is not None else ""
                line += f", **集中度 暂不可计算**（总资产或汇率不可用，请勿估算占比{mv}）"
        elif value is not None:
            line += f", {base} 市值 {money(value, base)}"
        # 压力测试用绝对金额，两种 lens 都给：Risk 模板的最坏情景字段照抄它。
        # 2026-10-11：lens 关时没有市值数字，Risk 为填"跌 20% 整体损失 %"自己编占比
        # （虚构画像真值 16.7% 编成 1.7%，CIO 照抄"仓位仅占 1.7%"）
        if value is not None:
            a, b, c = (money(value * k, base) for k in (0.10, 0.20, 0.35))
            line += f"，现价再跌 10% / 20% / 35% 损失 {a} / {b} / {c}"
        lines.append(line)
        lines.extend(observation_lines)

    return "\n".join(lines) + "\n"
