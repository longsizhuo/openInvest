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
CONTAMINATION_CUTOFF = "2024-12-31"   # 旧 MiMo 自报(历史 CI 用)
HOLDOUT_START = "2025-01-01"          # 旧(MiMo)holdout 起点

# 经验探针(cutoff_probe.py,2026-06-28):deepseek-v4-flash 知道 2025-01 DeepSeek-R1,
# 不知道 2025-05 Claude 4 → effective cutoff ≈ 2025-01..04(晚于自报,印证 arXiv:2403.12958)。
# 含义:① 2025-01..04 段对 DeepSeek 已污染 → 干净 holdout 须从 2025-05 后起;② 昨晚那轮闭环
# CI(2025-01..2026-03)头部被污染——但污染【抬高】业绩,委员会仍亏 → "委员会跑输"结论反而更稳。
DEEPSEEK_EFFECTIVE_CUTOFF = "2025-04-30"        # 保守上界(知道到 ~early 2025)
CLEAN_HOLDOUT_START_DEEPSEEK = "2025-06-01"     # cutoff + buffer:M3 LLM 回测须从此起才干净

INIT_CASH_CNY = 100_000.0
WEEKLY_STEP_DAYS = 7
FORWARD_WINDOWS = ("30d", "90d")

# 输出根(可复现产物 + 决策审计)
import os as _os

OUT_DIR = _os.path.join(_os.path.dirname(__file__), "out")
