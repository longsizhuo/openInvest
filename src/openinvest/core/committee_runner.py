"""committee_runner — 兼容 façade（拆分后薄壳）。

实现已按职责拆到 core/runner/ 下 5 个子模块；本文件 re-export 全部符号，
保持 `from core.committee_runner import X` 对所有历史 X 可用（3 个 entry + 测试零改）。
分层契约（import-linter）：entry → committee_runner → core.runner.* → core.committee，
entry 不直接 import core.committee，allow_indirect 放行，契约 trivially 续绿。
"""
from openinvest.core.runner.event_brief import *  # noqa: F401,F403
from openinvest.core.runner.loaders import *  # noqa: F401,F403
from openinvest.core.runner.intervention import *  # noqa: F401,F403
from openinvest.core.runner.session import *  # noqa: F401,F403
from openinvest.core.runner.coordinator import *  # noqa: F401,F403

# 转手 re-export core.committee（entry 经 service layer 用这些，避免直 import core.committee
# 违反 lint-imports 契约：cmd_save_committee 提 ATR/regime/cio_memo；daily_report 用 load_backup_cny）
from openinvest.core.committee import (  # noqa: F401
    atr_defense_from_text,
    load_backup_cny,
    load_wealth_context_view,
    parse_cio_memo,
    regime_label_from_text,
    run_committee,
    run_macro_view,
)
