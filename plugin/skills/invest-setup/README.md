# invest-setup skill — first-time onboarding

Modeled on the [OpenClaw `convex-setup-auth`](https://github.com/openclaw/clawhub/tree/main/.agents/skills/convex-setup-auth)
pattern — **first-time installation/onboarding lives in its own skill**, split off
from the day-to-day [`../invest/`](../invest/) skill.

## Why the split

The previous single `skill/SKILL.md` was 209 lines mixing 4 responsibilities:
onboarding (one-time) + daily usage (high-frequency) + Web API endpoints
(medium-frequency) + error handling (occasional). The agent paid that many tokens on
every startup.

Following OpenClaw's actual practice: **one skill = one working scenario**. So setup
was split out — the agent only loads invest-setup when `doctor` returns
`needs_setup`; otherwise it loads only invest.

See the detailed ADR in [`docs/wiki/11-rl-training.md`](../../docs/wiki/11-rl-training.md),
"Prompt organization" section.

## Directory layout

```
skills/invest-setup/
├── SKILL.md                    ← invest-setup main entry, with frontmatter trigger
├── scripts/
│   └── run.sh                  ← symlink → ../../invest/scripts/run.sh (reuse)
├── references/
│   └── onboarding-detailed.md  ← symlink → ../../invest/references/onboarding.md
└── README.md                   ← you are reading this
```

scripts/ + references/ reuse the invest skill's implementation via symlinks to avoid
duplicate maintenance.

## Installing into ~/.claude/skills/

`../install.sh` installs both skills in one go:

```bash
cd $INVEST_HOME && bash skills/install.sh
```

After installation:
- `~/.claude/skills/invest/` ← `skills/invest/`
- `~/.claude/skills/invest-setup/` ← `skills/invest-setup/`

At load time the agent picks a skill based on the trigger phrases in the
frontmatter description:
- "set up invest / 帮我初始化 invest" / `doctor` returns `needs_setup` → invest-setup
- "show portfolio / 看持仓" / "run committee / 跑委员会" / "analyze X / 分析 X" → invest

## When to Use / When NOT to Use

The SKILL.md frontmatter already spells out the exact conditions, so the agent won't
mix them up. Quick summary:

| | Use invest-setup | Use invest |
|---|---|---|
| First run | ✅ | ❌ |
| memory / user_profile missing | ✅ | ❌ |
| User says "reset / 重新配置" | ✅ | ❌ |
| Already onboarded, viewing holdings | ❌ | ✅ |
| Running a committee decision | ❌ | ✅ |
| Buy/sell / ledger changes | ❌ | ✅ |
