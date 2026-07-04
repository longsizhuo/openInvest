"""loaders — 所有 production entry 必经的 shared input loader（从 core/committee.py 拆分，逻辑逐字不变）。

职责：`load_wealth_context_view`（读 user.md.wealth_context + portfolio cash →
WealthContextOfficer view）+ `load_backup_cny`（读 emergency_buffer_cny → off-portfolio
兜底金额）。两者都 graceful 退化，永不阻断主流程。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from openinvest.core.committee.views import run_wealth_context_view
from openinvest.core.memory_store import MemoryStore

log = logging.getLogger(__name__)


# ============================================================================
# Shared Input Loaders — 所有 production entry 必经层
# ============================================================================
# 防漂移核心：所有"跨 entry 共享的输入"在这里统一定义。
# daily_report / committee_runner / backtest_committee / web_api 全用这些 loader,
# 永远不要在 entry 层重复读 user.md / portfolio.md / event_store。
#
# 加新的 cross-entry 参数（类似 wealth_context_view / event_brief）时：
#   1. 在 run_committee() 加 explicit 参数（默认 ""）
#   2. 在这里加一个 load_<name>() helper, graceful 退化空字符串
#   3. **所有 entry 调用 run_committee 之前先调 load_<name>()**
#   4. 加 e2e contract test 验证每个 entry 都传了
#
# 2026-05-15 漂移事故：wealth_context_view 只接了 prompt + 测试，没接调用链
# → 三个月 user.md 的 wealth_context 没进入任何 production 决策。
# Import rule（pyproject.toml）已禁止 entry 直接 import run_committee 跳过这层。
# ============================================================================


def load_wealth_context_view() -> str:
    """读 user.md.wealth_context + portfolio cash → WealthContextOfficer view。

    Graceful: 任何异常都返回空 str, 委员会照常跑（Risk Officer 退化为只看
    portfolio cash 的老逻辑）。
    """
    try:
        from openinvest.core.memory_store import MemoryStore
        from openinvest.core.portfolio_manager import PortfolioManager
        store = MemoryStore()
        user_doc = store.read("user")
        wealth_context = user_doc.metadata.get("wealth_context") if user_doc else None
        pm = PortfolioManager()
        portfolio_cash_cny = pm.cash_amount("CNY")
        return run_wealth_context_view(wealth_context, portfolio_cash_cny)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_wealth_context_view graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def load_backup_cny(pm: Optional[Any] = None) -> float:
    """读 user.md.wealth_context.emergency_buffer_cny → off-portfolio 兜底金额（CNY）。

    用于 portfolio_summary_text 的"真实总财富占比"注释，三路径（cron / skill /
    service）单一可信源。正式字段是 `emergency_buffer_cny`（WealthContextRequest /
    invest-setup / GUI 写入）；历史上 daily_report / skill 误读不存在的
    `backup_amount_cny`，导致 backup_cny 恒为 0、注释从不渲染 —— 本 loader 修掉这个
    key 漂移并消除三处重复。

    Graceful: 读不到 / 异常 → 0.0（退化到"无兜底"逻辑，不阻断主流程）。

    Args:
        pm: 复用已有 PortfolioManager 的 store（避免重复 new MemoryStore）；
            None 时自建 MemoryStore() 读 user.md。
    """
    try:
        store = pm.store if pm is not None else MemoryStore()
        user_doc = store.read("user")
        wealth_context = user_doc.metadata.get("wealth_context") if user_doc else None
        if not wealth_context:
            return 0.0
        return float(wealth_context.get("emergency_buffer_cny", 0) or 0)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_backup_cny graceful 退化 0.0: {type(e).__name__}: {e}")
        return 0.0


__all__ = [
    "load_wealth_context_view",
    "load_backup_cny",
]
