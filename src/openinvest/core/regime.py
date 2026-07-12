"""市场 Regime 分类 —— 实现已迁移至 openinvest.calc.regime（计算层，ADR-026）

薄壳 façade：保持全部历史 import 路径可用，entry / 测试零改。
monkeypatch 请钉 openinvest.calc.regime.* 实现命名空间，patch 本模块属性打不到实现。
"""
from openinvest.calc.regime import *  # noqa: F401,F403
