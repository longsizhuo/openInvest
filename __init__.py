"""Hermes plugin 注册器（仓库根，与 plugin.yaml 相邻——Hermes 约定）。

只做一件事：把 plugin/skills/ 下的 agentskills.io 标准 skill 注册进 Hermes。
MCP server（18 工具）是 Hermes 的 config.yaml 扩展面，plugin API 无法代注册
（官方文档明确 MCP 属"非 Python 扩展面"），安装后提示用户加三行配置。

本文件对其他生态零影响：Claude/Codex plugin 的 source 是 ./plugin 子目录，
wheel 只打包 src/，谁都不会碰到仓库根的这两个文件。
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def register(ctx):
    skills_dir = Path(__file__).parent / "plugin" / "skills"
    for child in sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []:
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
            log.info(f"[openinvest] 注册 skill: {child.name}")
    log.info(
        "[openinvest] MCP 工具（15 个）需在 ~/.hermes/config.yaml 加: "
        "mcp_servers.openinvest → command: uvx, args: [openinvest, mcp], "
        "env: {INVEST_HOME: ~/openInvest}"
    )
