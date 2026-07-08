"""Investment Committee 编排 - 4 角色（Quant / Macro / Risk Officer / CIO）

设计要点：
- 信息分隔（每个 agent 只看自己领域的数据）
- 多轮 cross-challenge：Round 2..N agent 看对方上一轮输出后调整自己（max_debate_rounds 控制）
- Round 1 / Round 2..N 内部 Quant 和 Risk **真并行**（ThreadPoolExecutor）
- 收敛判定：连续 2 轮 SIGNAL+STRENGTH 都没变 → 提前退出
- progress_callback：每个 stage emit 一次（启动 / Round N / converged / cio_done）

LLM 调用数：
- max_debate_rounds=1（旧默认，daily_report 用）：5 LLM × N 资产
- max_debate_rounds=4（live 真讨论模式）：最多 (1 + 4*2 + 1) = 10 LLM，
  但收敛后会提前退出，平均 6-8 LLM

拆包说明（2026-06-15，#56 同款薄壳 façade）：本文件原是扁平 core/committee.py，
现按职责拆到本包的子模块（agent_io / cio_parse / views / debate /
persist）。本 __init__ re-export 全部历史符号（含下划线名 + 模块常量），保持
`from core.committee import X` 与 `core.committee.X` 属性访问对所有历史 X 仍可用，
entry / service / 测试 / 脚本零改。注意：run_committee 实际定义在 debate.py，
其内部解析的 _create_agent / _persist 等名字在 debate 命名空间——测试/脚本要 patch
的是 core.committee.debate.* 而非本 façade 属性。

import 顺序（依赖自底向上）：agent_io → cio_parse → persist → views → debate。
"""
from openinvest.core.committee.agent_io import *  # noqa: F401,F403
from openinvest.core.committee.cio_parse import *  # noqa: F401,F403
from openinvest.core.committee.persist import *  # noqa: F401,F403
from openinvest.core.committee.views import *  # noqa: F401,F403
from openinvest.core.committee.debate import *  # noqa: F401,F403

from openinvest.core.committee.agent_io import __all__ as _agent_io_all
from openinvest.core.committee.cio_parse import __all__ as _cio_parse_all
from openinvest.core.committee.persist import __all__ as _persist_all
from openinvest.core.committee.views import __all__ as _views_all
from openinvest.core.committee.debate import __all__ as _debate_all

# 包级 __all__ = 各子模块 __all__ 的并集（保持历史导出面）
__all__ = list(
    _agent_io_all
    + _cio_parse_all
    + _persist_all
    + _views_all
    + _debate_all
)
