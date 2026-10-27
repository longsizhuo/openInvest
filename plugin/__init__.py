"""Hermes plugin registrar (next to plugin.yaml, per Hermes convention).

Registers the agentskills.io skills under ./skills. The MCP server is a Hermes config.yaml surface the
plugin API can't register; after-install.md (shown by Hermes after install) and the log line below
print the entry to add (pinned via run.sh).
Lives in plugin/ so a subdir install (and the catalog's subdir pin) scans and installs only the plugin,
not the backend's pyproject. The repo-root copy stays for installs made before the move.

Context for the agent (2026-10-27, fresh-user test in a clean Hermes): Hermes never puts the MCP
server's `instructions` into the prompt, so the agent only reached for openInvest when the user said
"portfolio"; a question about a held ticker went straight to web search. So we add
- a fixed routing rule in the system prompt (cache-safe, survives compression), and
- a `pre_llm_call` hook that injects `run.sh brief --text` (offline ledger digest: holdings, today's
  verdicts, pending queues) on a session's first turn and again only when the digest changes.
"""
import logging
import os
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

RUN_SH = Path(__file__).resolve().parent / "skills" / "invest" / "scripts" / "run.sh"
ROUTING_RULE = (
    "openInvest is installed: the user's own investment ledger, live prices and a 4-role committee. "
    "For any question about stocks, funds, ETFs, gold, crypto, markets, macro, buying or selling, or the "
    "user's money, call the openinvest tools first (status / live_prices; the symbol may already be a holding) "
    "and only then add web search. Load the openinvest:invest skill before running a committee."
)
BRIEF_TTL_SECONDS = 300
BRIEF_TIMEOUT_SECONDS = 15
SKIP_PLATFORMS = {"subagent", "cron"}  # our own cron prompts call the tools explicitly; children inherit context

_brief_cache = {"at": float("-inf"), "text": None}
# ponytail: per-session "last injected" lives in this process; a gateway restart re-injects once per session
_last_injected: dict = {}


def _brief_text(invest_home):
    """`run.sh brief --text`, cached for BRIEF_TTL_SECONDS. None on any failure (older backend, timeout)."""
    now = time.monotonic()
    if now - _brief_cache["at"] < BRIEF_TTL_SECONDS:
        return _brief_cache["text"]
    env = dict(os.environ)
    if invest_home:
        env["INVEST_HOME"] = os.path.expanduser(str(invest_home))
    text = None
    try:
        r = subprocess.run(["bash", str(RUN_SH), "brief", "--text"], capture_output=True, text=True,
                           timeout=BRIEF_TIMEOUT_SECONDS, env=env)
        out = r.stdout.strip()
        if r.returncode == 0 and out.startswith("[openInvest]"):
            text = out
        else:
            log.info("[openinvest] brief unavailable (rc=%s): %s", r.returncode, (out or r.stderr)[-200:])
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("[openinvest] brief unavailable: %s", e)
    _brief_cache.update(at=now, text=text)
    return text


def _make_pre_llm_call(invest_home):
    def pre_llm_call(session_id=None, platform=None, **_):
        if platform in SKIP_PLATFORMS:
            return None
        text = _brief_text(invest_home)
        if not text or _last_injected.get(session_id) == text:
            return None
        if len(_last_injected) > 1000:  # ponytail: long-lived gateway, bound the map instead of tracking session ends
            _last_injected.clear()
        _last_injected[session_id] = text
        return {"context": text}
    return pre_llm_call


def register(ctx):
    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()) if skills_dir.is_dir() else []:
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
            log.info(f"[openinvest] registered skill: {child.name}")

    # Older Hermes builds lack these APIs; skills + MCP keep working without them.
    invest_home = None
    if hasattr(ctx, "get_config"):
        try:
            invest_home = ctx.get_config("invest_home")
        except Exception:  # noqa: BLE001  config read must never break plugin load
            invest_home = None
    if hasattr(ctx, "register_system_prompt_section"):
        try:
            ctx.register_system_prompt_section("openinvest.routing", ROUTING_RULE)
        except (TypeError, ValueError) as e:
            log.info("[openinvest] system prompt section not registered: %s", e)
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("pre_llm_call", _make_pre_llm_call(invest_home))

    # MCP 走本插件自带的 run.sh：它把后端钉在 release-please 盖章的版本（不跑 PyPI 上最新的），
    # 插件更新后 run.sh 跟着更新，这段配置不用改。拆两条：Hermes 的 untrusted 按 server 生效、每次都问、
    # 没有"永久允许"，只有动钱那条（ledger）留 untrusted，cron 要用的 agent 那条给 full trust。
    # 不写 INVEST_HOME：Hermes 不经 shell 起进程，~ 不会展开；run.sh 自己默认 INVEST_HOME=$HOME/openInvest
    log.info(
        "[openinvest] MCP tools: add to ~/.hermes/config.yaml (after-install.md has the commands)\n"
        "mcp_servers:\n"
        "  openinvest:          # reads, news queue, committee — full trust so cron jobs can run\n"
        "    command: bash\n"
        f'    args: ["{RUN_SH}", "mcp"]\n'
        "    env: {INVEST_MCP_SCOPE: agent}\n"
        "  openinvest_ledger:   # tools that move money — Hermes asks before each call\n"
        "    command: bash\n"
        f'    args: ["{RUN_SH}", "mcp"]\n'
        "    env: {INVEST_MCP_SCOPE: ledger}\n"
        "    trust: untrusted"
    )
