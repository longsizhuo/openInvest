"""symbol 字符串归一原语（中立层——任何层都可 import，不撞分层契约）。

issue #179 P1-B④：safe_symbol 正则此前在 core/committee/persist.py 一处定义 +
9 处手抄 inline（mcp_server ×2 / decision_ledger / coordinator / intervention /
verdict_review / remote_dispatch / committee_cmds / capabilities.tools）。同类
漂移咬过一次（文件名口径两处不一致 → 断点续跑探测不到文件静默重跑），
收敛到这里做单一可信源；persist.py re-export 保持全部历史 import 路径可用。
"""
from __future__ import annotations

import re


def safe_symbol(symbol: str) -> str:
    """symbol → 文件名/路径安全名（GC=F → GC_F，510300.SS → 510300_SS）。

    用途：committee/backtest transcript 落盘名、断点续跑存在性探测、
    decision ledger join 键。改这个正则 = 改全仓文件名口径，三思。
    """
    return re.sub(r"[^a-zA-Z0-9_-]", "_", symbol or "asset")

# 完整历史导出面（含下划线名/常量）——façade `import *` 的完备性依赖本列表
__all__ = [
    "safe_symbol",
]
