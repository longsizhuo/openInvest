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
    today = datetime.now().strftime("%Y-%m-%d")

    last_payday = str(pm.user.get("last_payday", "1970-01-01"))
    last_dt = datetime.strptime(last_payday, "%Y-%m-%d")
    today_dt = datetime.now()

    # 同月已经入账过就跳过（防双跑）
    if last_dt.year == today_dt.year and last_dt.month == today_dt.month:
        return {"status": "skipped", "reason": "already_paid_this_month",
                "last_payday": last_payday}

    income = float(pm.user.get("monthly_income_cny", 0))
    expense = float(pm.user.get("monthly_expenses_cny", 0))
    net = income - expense

    if net <= 0:
        return {"status": "skipped", "reason": "net_income_not_positive", "net": net}

    pm.add_income(net, today)
    return {"status": "success", "net_income_cny": net, "payday": today}


if __name__ == "__main__":
    print(run())
