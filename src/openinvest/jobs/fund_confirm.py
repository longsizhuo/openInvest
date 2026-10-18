"""fund_confirm —— 场外基金在途申购单按净值自动确认（ADR-027 D5，场外基金两段式的第二段）

``PortfolioManager.place_fund_order`` 下单时现金已扣、订单记成 ``placed``（在途，总资产按面值含它）；本 job 工作日晚间
每 30 分钟（``fund_confirm.yml``）对每笔 ``placed`` 且定价日 ``nav_date <= 今天``（北京时间）的单子调
``confirm_fund_order``：取 ``nav_date`` 当天的单位净值，``units = (金额 − 费用) / 净值``，订单转 ``confirmed``。

- **净值没公布** → 单子原样留着（``pending_nav``），下一次再试，不算失败。QDII / FOF 的净值常晚一两天公布，
  靠连续几晚的重试自然接上；预计确认日过了还没取到，status 会提示（``pending_fund_orders_line``）。
- **幂等（ADR-016 红线 4）**：确认是对 ``order_id`` 的 ``placed → confirmed`` CAS，检查与写入在同一个 with_portfolio_tx 里——
  job 重跑 / 与手动 ``confirm_fund_order`` 并发都只有一个生效，另一个读到 ``already_confirmed``。
- **失败**：单笔确认抛异常（账本损坏、金额非法……）不挡其余订单，跑完整批后统一抛 RuntimeError → runner 记
  job_runs ``failed``。注意 job_watchdog 目前不会据 ``failed`` 行告警（只报停摆 / 卡死 / 超时），要看 job_runs。
- **申购费**：自动确认按 fee=0（job 不知道券商实际收了多少）；要记费用，到点前手动 ``confirm_fund_order --nav N --fee F``。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from openinvest.calc.fund_rules import CN_TZ
from openinvest.core.portfolio_manager import MemoryNotInitialized, PortfolioManager

log = logging.getLogger(__name__)

SOURCE = "fund_auto_confirm"


def run(*, today: Optional[str] = None, pm: Optional[PortfolioManager] = None) -> Dict[str, Any]:
    """扫一遍在途单。调度器裸调 ``run()``；``today`` / ``pm`` 仅测试注入。"""
    today = today or datetime.now(CN_TZ).date().isoformat()
    try:
        pm = pm or PortfolioManager()
    except MemoryNotInitialized:   # 还没 init 的数据目录：没有账本就没有在途单，不算失败
        return {"status": "skipped", "reason": "memory_not_initialized"}
    counts = {"confirmed": 0, "pending_nav": 0, "already": 0, "not_due": 0}
    details: List[Dict[str, Any]] = []
    errors: List[str] = []
    for o in pm.pending_fund_orders():
        oid, sym, nav_date = str(o["order_id"]), str(o["symbol"]), str(o.get("nav_date") or "")
        if nav_date > today:
            counts["not_due"] += 1
            continue
        try:
            r = pm.confirm_fund_order(oid, source=SOURCE)
        except Exception as e:  # noqa: BLE001  单笔失败不挡其余订单，批末统一抛
            errors.append(f"{sym} {oid}: {type(e).__name__}: {e}")
            log.error("[fund_confirm] %s %s 确认失败: %s", sym, oid, e)
            continue
        st = r.get("status")
        if st == "ok":
            counts["confirmed"] += 1
            details.append({"symbol": sym, "order_id": oid, "units": r.get("units"), "nav": r.get("nav"),
                            "nav_date": r.get("nav_date")})
            log.info("[fund_confirm] %s 确认 %s 份 @ %s（净值日 %s）", sym, r.get("units"), r.get("nav"), r.get("nav_date"))
        elif st == "pending_nav":
            counts["pending_nav"] += 1
            log.info("[fund_confirm] %s 定价日 %s 的净值还没公布（%s），下次再试", sym, nav_date, r.get("reason"))
        else:   # already_confirmed / cancelled：别处先到了
            counts["already"] += 1
    if errors:
        raise RuntimeError(f"fund_confirm: {len(errors)} 笔在途单确认失败（其余已处理 {counts}）：" + "；".join(errors))
    return {"status": "success", "date": today, **counts, **({"confirmed_orders": details} if details else {})}


if __name__ == "__main__":
    print(run())
