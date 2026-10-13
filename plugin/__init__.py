"""Hermes plugin registrar (next to plugin.yaml, per Hermes convention).

Registers the agentskills.io skills under ./skills. The MCP server is a Hermes config.yaml surface the
plugin API can't register; the log line after install prints the config.yaml entry to add (pinned via run.sh).
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
    # MCP 走本插件自带的 run.sh：它把后端钉在 release-please 盖章的版本（不跑 PyPI 上最新的），
    # 插件更新后 run.sh 跟着更新，这段配置不用改。trust: untrusted 让 Hermes 在写账本的工具前先问用户。
    # 不写 env：Hermes 不经 shell 起进程，~ 不会展开；run.sh 自己默认 INVEST_HOME=$HOME/openInvest
    run_sh = Path(__file__).resolve().parent / "skills" / "invest" / "scripts" / "run.sh"
    log.info(
        "[openinvest] MCP tools: add to ~/.hermes/config.yaml\n"
        "mcp_servers:\n"
        "  openinvest:\n"
        "    command: bash\n"
        f'    args: ["{run_sh}", "mcp"]\n'
        "    trust: untrusted"
    )
