# scripts/skill_cmds/

`scripts/skill.py` 的命令实现子包。skill.py 已退化为薄壳 façade（顶部 `from .子模块 import *` 重新对齐符号面 + 保留 `main()` 与 `__main__` guard），具体 cmd 实现按职责拆到本包 5 个子模块。

## 子模块职责

| 模块 | 职责 |
|------|------|
| `_helpers.py` | 共享工具：`_print_json`（直写真实 stdout 出 JSON）、`_safe_close`（拉单 symbol 收盘价）、`_now_iso_local`（本地 ISO 时戳，dead 副本随包保留） |
| `analysis_cmds.py` | 只读分析：`status` / `strategy` / `history` / `what_if` / `correlate` / `live_prices` / `event_check`。自有 ROOT 承接 test 对 `cmd_status` 的 no-op patch |
| `committee_cmds.py` | 委员会：`prepare_committee`（Coordinator）/ `run_committee`（Direct）/ `save_committee`。自有 ROOT 拼 transcript_path |
| `portfolio_cmds.py` | 写操作：`deposit` / `withdraw` / `buy` / `sell` / `delete_holding` + `_resolve_pm` |
| `lifecycle_cmds.py` | onboarding：`doctor` / `init` + 持仓 LLM 解析。**必须自有 ROOT**（test patch 重定向主目标） |

## 注意

- 每个读 ROOT 的子模块各自 `ROOT = Path(__file__).resolve().parents[2]`（= repo 根，与 façade 等值）；test patch 必须命中 cmd 所在模块的 ROOT，patch façade 的 ROOT 不生效。
- 所有 def/常量逐字搬自 skill.py（行为保持）；函数内的延迟 import（pandas/yfinance/openai 等）保留在函数体，不上提。
- `__init__.py` 不做 re-export（façade 独家承担），避免双重 `import *` 混淆。
