"""openinvest.calc —— 计算层（T0：纯计算，域中立）

本包内模块只做确定性计算：同输入→同输出，禁止网络 / 文件 / SQLite /
`datetime.now()` / LLM 调用。唯一放行的依赖是 `openinvest.core.config`
（确定性 yaml 配置，视为可注入参数）。机器强制见 pyproject [tool.importlinter]
纯度契约 + CI grep 守卫；分层契约全文见 docs/wiki/adr/026。

monkeypatch 注意：旧路径（core/regime、utils/market_metrics 等）只是薄壳
façade——patch 请钉本包的实现命名空间（openinvest.calc.<module>.<symbol>）。
"""
