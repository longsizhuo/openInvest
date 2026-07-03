"""committee capability — 4 角色 AI 投资委员会辩论

每个角色是自包含目录 `capabilities/committee/<role>/`:
- <role>.py — prompt builder 实现
- <role>.md — 主 prompt 模板
- <role>_<round>.md — 可选：其他轮次 prompt（如 rebuttal）

直接按子包导入角色：
    from capabilities.committee.cio import build_cio_prompt
    from capabilities.committee.quant import build_quant_prompt

不要从本 __init__.py 导入——这里不做 re-export，避免一个角色
prompt 文件缺失级联崩溃全部 committee import。
"""
