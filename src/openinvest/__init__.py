"""openInvest —— 面向 agent 的投资决策 runtime。

src/ layout（issue #133 PyPI 路线）：全部后端包收进 openinvest.* 命名空间，
发 PyPI 不再往用户 site-packages 撒 core/db/utils 这种通用名顶层包。
"""
# 单一可信源是 pyproject.toml（release-please 管），这里从安装元数据读，不再硬编码
try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("openinvest")
except Exception:  # 未安装形态（裸 sys.path 挂 src/）拿不到元数据
    __version__ = "0.0.0.dev0"
