"""每月 1 号 09:00 把净收入入账

替代旧 PortfolioManager.process_income 的隐式副作用。
现在显式：只在 cron 触发或 NapCat 手动 `/payday` 时执行。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from core.portfolio_manager import PortfolioManager


def run() -> Dict[str, Any]:
    pm = PortfolioManager()
    today_dt = datetime.now()
    today = today_dt.strftime("%Y-%m-%d")
    month_key = today_dt.strftime("%Y-%m")  # 幂等键：每月一次

    # 粗检查（向后兼容旧 last_payday 字段，且做快速路径）：同月已入账直接跳过。
    last_payday = str(pm.user.get("last_payday", "1970-01-01"))
    try:
        last_dt = datetime.strptime(last_payday, "%Y-%m-%d")
        if last_dt.year == today_dt.year and last_dt.month == today_dt.month:
            return {"status": "skipped", "reason": "already_paid_this_month",
                    "last_payday": last_payday}
    except ValueError:
        pass  # last_payday 脏数据 → 交给下面的原子 claim 兜底

    income = float(pm.user.get("monthly_income_cny", 0))
    expense = float(pm.user.get("monthly_expenses_cny", 0))
    net = income - expense

    if net <= 0:
        return {"status": "skipped", "reason": "net_income_not_positive", "net": net}

    # 原子幂等闸：claim 本月 key（check+append 同一把 fcntl 锁），杜绝 cron + 手动
    # /payday 并发双跑把月度净收入重复入账。上面的 last_payday 粗检查是 check-then-
    # act：add_income 改 cash 与 update_fields 改 last_payday 是两次分离的写，并发两
    # 路都能过同月检查 → 重复记账（与 #62 record_external_trade 修的 TOCTOU 同类）。
    # claim 失败（已被另一路占用）则跳过；add_income 抛错再 unclaim 让下次能重试。
    if not pm.store.state_claim("paydays_applied", month_key):
        return {"status": "skipped", "reason": "already_paid_this_month",
                "month": month_key}
    try:
        pm.add_income(net, today)
    except Exception:
        pm.store.state_unclaim("paydays_applied", month_key)
        raise
    return {"status": "success", "net_income_cny": net, "payday": today}


if __name__ == "__main__":
    print(run())
