"""symbol 字符串归一原语（中立层——任何层都可 import，不撞分层契约）。

issue #179 P1-B④：safe_symbol 正则此前在 core/committee/persist.py 一处定义 +
9 处手抄 inline（mcp_server ×2 / decision_ledger / coordinator / intervention /
verdict_review / remote_dispatch / committee_cmds / capabilities.tools）。同类
漂移咬过一次（文件名口径两处不一致 → 断点续跑探测不到文件静默重跑），
收敛到这里做单一可信源；persist.py re-export 保持全部历史 import 路径可用。
"""
from __future__ import annotations

import datetime as _dt
import re


def is_closed_weekend(symbol: str, date_str: str) -> bool:
    """date_str 是周六/日，且 symbol 周末不交易（非 FX ``=X``、非加密 ``-USD``）。

    全仓唯一的周末口径（2026-10 D8）：
    - 写库：这种日期的 bar 是幽灵（tz 错位造的），market_store 拒写；
    - 复盘：这种日期的决议只能拿周五收盘当基准，是周五样本的重复（weekend_dup），
      verdict_review / path_review / export_accuracy 的命中率与校准一律剔除（行保留）。
    ponytail: 只看星期几，不查交易所假日历——假日重复样本另算。
    """
    s = (symbol or "").upper()
    if s.endswith("=X") or s.endswith("-USD"):
        return False
    try:
        return _dt.date.fromisoformat(date_str).weekday() >= 5
    except (ValueError, TypeError):
        return False


def safe_symbol(symbol: str) -> str:
    """symbol → 文件名/路径安全名（GC=F → GC_F，510300.SS → 510300_SS）。

    用途：committee/backtest transcript 落盘名、断点续跑存在性探测、
    decision ledger join 键。改这个正则 = 改全仓文件名口径，三思。
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", symbol or "asset")

# 完整历史导出面（含下划线名/常量）——façade `import *` 的完备性依赖本列表
__all__ = [
    "is_closed_weekend",
    "safe_symbol",
]
