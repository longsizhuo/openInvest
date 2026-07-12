"""策略级评估指标 —— 实现已迁移至 openinvest.calc.strategy_metrics（计算层，ADR-026）

薄壳 façade：保持全部历史 import 路径可用，entry / 测试零改。
monkeypatch 请钉 openinvest.calc.strategy_metrics.* 实现命名空间，patch 本模块属性打不到实现。
"""
from openinvest.calc.strategy_metrics import *  # noqa: F401,F403
