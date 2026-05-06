"""交易日历检查 - 跳节假日 / 周末

每个资产在 strategy.md 里声明 exchange_calendar:
- NDQ.AX → XASX (澳交所，覆盖澳洲国庆/ANZAC/复活节等)
- 伦敦金 (浙商积存金) → XLON (英国银行假日，与 LBMA 金价定盘日对齐)

非交易日跳过该资产 committee：技术信号在停盘日没新数据，跑了也只是把昨天的
分析重复一遍，浪费 LLM token + 误导用户。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import exchange_calendars as xcal


def is_trading_day(calendar_code: str, on_date: Optional[date] = None) -> bool:
    """on_date 是不是 calendar_code 交易所的开市日。on_date=None 用今天 (Asia/Shanghai)。

    未知 calendar_code 一律 return True，避免误伤——配置错应该让用户看到资产
    照常出报告，而不是默默 skip。
    """
    if on_date is None:
        on_date = datetime.now().date()
    try:
        cal = xcal.get_calendar(calendar_code)
    except Exception:
        return True
    return bool(cal.is_session(on_date.isoformat()))


def closed_reason(calendar_code: str, on_date: Optional[date] = None) -> str:
    """给跳过日志用的简短原因字符串"""
    if on_date is None:
        on_date = datetime.now().date()
    weekday = on_date.weekday()  # 0=Mon, 6=Sun
    if weekday >= 5:
        return f"{calendar_code} 周末休市"
    return f"{calendar_code} 节假日休市"
