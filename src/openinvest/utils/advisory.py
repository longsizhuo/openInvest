"""顾问模式（INVEST_ADVISORY_MODE）判定 —— 单一可信源。

mcp_server.py 的工具闸和 core/runner/session.py 的委员会 orchestrator 都要判断
是否处于顾问模式；此前两处各自手写 `os.environ.get(...).strip()`（非空字符串即
真），导致 `INVEST_ADVISORY_MODE=0` / `=false` 也会误开顾问模式。这里对齐仓库
其余 bool env 的写法（见 jobs/price_sentinel.py 的 INVEST_SENTINEL_DRY_RUN）。
"""
from __future__ import annotations

import os


def is_advisory_mode() -> bool:
    return os.environ.get("INVEST_ADVISORY_MODE", "").strip().lower() in ("1", "true")
