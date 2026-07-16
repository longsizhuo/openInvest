# invest skill — daily usage

The **daily-usage agent skill** for openInvest. View portfolio / run the committee / add or trim
positions / "btw" correlation analysis.

> **First-time installation** goes through the other skill: [`../invest-setup/`](../invest-setup/) —
> when `doctor` returns `needs_setup`, the agent automatically loads it and runs the 5-question
> onboarding; only after that does this skill take over.

## Directory layout

```
skills/invest/
├── SKILL.md          ← agent trigger guide (decision tree / subcommands / Web API endpoints)
├── scripts/
│   └── run.sh        ← subcommand dispatcher (internally pulls the backend from PyPI via `uvx openinvest`; update = `run.sh update`)
├── references/
│   ├── committee-protocol.md     ← detailed Coordinator-path stages
│   ├── two-paths.md              ← Coordinator vs Direct differences
│   ├── adding-assets.md          ← adding a new tracked symbol
│   ├── troubleshooting.md        ← read when doctor is all green but things still fail
│   └── onboarding.md             ← detailed 5-question flow (also referenced by the invest-setup skill)
└── README.md         ← you are here
```

## Installation

The parent directory's `../install.sh` installs both skills in one go:

```bash
cd $INVEST_HOME              # default ~/openInvest
bash skills/install.sh        # installs both invest + invest-setup
```

`install.sh` installs into `~/.claude/skills/invest/` and `~/.claude/skills/invest-setup/`,
whose contents are symlinks pointing back at the source directory — updates to `SKILL.md` /
`scripts/` take effect immediately, no reinstall needed. The backend itself is distributed via
PyPI; update it with `run.sh update`.

## Workflow when changing the protocol

```bash
cd $INVEST_HOME
# 1. Edit SKILL.md / scripts/run.sh / references/*.md
vim skills/invest/SKILL.md
# 2. Test (the symlink is already live; no reinstall needed)
~/.claude/skills/invest/scripts/run.sh status
# 3. commit + push
git add skills/invest/ && git commit -m "..." && git push
# 4. Other devices pick it up immediately after git pull (symlinks unchanged)
#    The production server has invest-deploy.timer auto-git-pulling hourly
```

## Relationship with the invest-setup skill

| | invest (this skill) | invest-setup |
|---|---|---|
| When it triggers | Daily — "show portfolio" (看持仓), "analyze X" (分析 X), "add to a position" (加仓) | First time — "set up invest / 帮我初始化 invest", or `doctor` returns `needs_setup` |
| Frequency | High-frequency, continuous use | Once (retires after onboarding) |
| Internal scripts | its own `scripts/run.sh` | symlink → `../invest/scripts/run.sh` (reuse) |
| Internal references | its own 5 md files | `onboarding-detailed.md` symlink → `../invest/references/onboarding.md` |

The design follows OpenClaw's [`convex-setup-auth`](https://github.com/openclaw/clawhub/tree/main/.agents/skills/convex-setup-auth)
pattern: **one skill per single working scenario**.

## Custom installation path

```bash
CLAUDE_SKILLS_DIR=/some/other/path bash skills/install.sh
```

Usually unnecessary — Claude Code reads skills from `~/.claude/skills/<name>/` by default.

## Uninstall

```bash
rm -rf ~/.claude/skills/invest ~/.claude/skills/invest-setup
```

The repository itself is unaffected.

## Cross-agent compatibility

SKILL.md follows the [agentskills.io](https://agentskills.io) open standard and is theoretically
compatible with 35+ agent clients: Claude Code / Cursor / OpenCode / OpenHands / Cline / Goose /
Gemini CLI / Codex, etc. However, **install.sh hard-codes `~/.claude/skills/`** (the Claude Code
path); other clients may use different locations such as `~/.cursor/skills/` — fork users should
set the `CLAUDE_SKILLS_DIR` environment variable themselves.

OpenClaw users can use `clawhub install` (if this is ever published to the ClawHub registry).
The current lowest common denominator = grab the skills/ directory and run
`bash skills/install.sh` (no need to clone the backend; `run.sh` internally pulls it from PyPI
via `uvx openinvest`).

## Also read

- The full SKILL.md protocol: [`SKILL.md`](SKILL.md)
- Project architecture wiki: [github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
- Two-path decision: [`references/two-paths.md`](references/two-paths.md)
