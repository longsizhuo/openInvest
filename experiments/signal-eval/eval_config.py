"""eval_config — 评测单一可信源(优化计划 v5)。

Q1 / Q2 / harness 全部 import 这里,杜绝 ASSETS / CUTOFF 在多个脚本各写一份导致
"对照跑在不同资产/窗口上而无人察觉"(repo 历史上的 flip-flop 结构根因之一)。
"""
from __future__ import annotations

# 用户的养老定投篮子(Q2 篮子择时测试对象)——黄金 / A股 / 纳指
BASKET_ASSETS = ["GC=F", "510300.SS", "NDQ.AX"]

# LLM 训练 cutoff:旧 MiMo 自报 2024-12-31。⚠ 已切 DeepSeek-v4-flash,M3 跑 LLM 前必须
# 重探 effective cutoff(arXiv:2403.12958 effective>reported 常态),否则 holdout 边界失锚。
# Q2 本身无 LLM(纯确定性 regime→forward),不受 memorization 影响,可用全历史;受影响的是
# regime 阈值的 in-sample tuning(见 Q2 脚本的 textbook-regime 干净读)。
CONTAMINATION_CUTOFF = "2024-12-31"
HOLDOUT_START = "2025-01-01"

INIT_CASH_CNY = 100_000.0
WEEKLY_STEP_DAYS = 7
FORWARD_WINDOWS = ("30d", "90d")

# 输出根(可复现产物 + 决策审计)
import os as _os

OUT_DIR = _os.path.join(_os.path.dirname(__file__), "out")
