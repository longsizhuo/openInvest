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


def is_no_verdict_mode() -> bool:
    """无裁决模式（INVEST_NO_VERDICT_MODE）：委员会只出正反理由，不出 verdict/金额/置信度。

    给公开演示实例用（未经许可不得向公众提供金融领域的确定性结论）。
    CIO 换成书记员（capabilities/committee/scribe），对外出口只放行辩论纪要。
    """
    return os.environ.get("INVEST_NO_VERDICT_MODE", "").strip().lower() in ("1", "true")
