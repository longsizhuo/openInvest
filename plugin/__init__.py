"""Hermes plugin registrar (next to plugin.yaml, per Hermes convention).

Registers the agentskills.io skills under ./skills. The MCP server is a Hermes config.yaml surface the
plugin API can't register; the log line after install tells the user the three lines to add.
Lives in plugin/ so a subdir install (and the catalog's subdir pin) scans and installs only the plugin,
not the backend's pyproject. The repo-root copy stays for installs made before the move.
"""
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def register(ctx):
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []:
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
            log.info(f"[openinvest] registered skill: {child.name}")
    log.info(
        "[openinvest] MCP tools: add to ~/.hermes/config.yaml "
        "mcp_servers.openinvest -> command: uvx, args: [openinvest, mcp], "
        "env: {INVEST_HOME: ~/openInvest}"
    )
