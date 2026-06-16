"""skill_cmds —— scripts/skill.py 的命令实现子包

历史上所有 skill 子命令（status / doctor / init / run_committee / 买卖现金 等）
都堆在单文件 scripts/skill.py 里。本包把它们按职责拆成 5 个子模块，
scripts/skill.py 退化为薄壳 façade（顶部 `from .子模块 import *` 重新对齐符号面，
保留 main() 与 __main__ guard）。

不在本 __init__ 做 re-export —— façade（scripts/skill.py）独家承担符号转手，
避免双重 import * 造成符号来源混淆。
"""
