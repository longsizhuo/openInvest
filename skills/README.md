# skills/ — openInvest agent skills

参考 [OpenClaw `.agents/skills/`](https://github.com/openclaw/openclaw/tree/main/.agents/skills)
模式，把所有 agent skill 集中放在一个父目录下。每个子目录 = 一个独立 skill。

## 当前 skills

| Skill | 何时用 | 频率 |
|---|---|---|
| [`invest/`](invest/) | 日常使用 —— 看持仓 / 跑委员会 / 加减仓 / 关联分析 | 高频持续 |
| [`invest-setup/`](invest-setup/) | 首次安装 / migrate / 重配 —— 5 问 onboarding | 一次性 |
| [`okf-frontmatter/`](okf-frontmatter/) | 维护 docs/wiki 文档（OKF frontmatter）+ 按 schema/endpoint/config 反查文档 | 改文档时 |

## 一键装两个 skill

```bash
cd $INVEST_HOME              # 默认 ~/openInvest
bash skills/install.sh
```

> 📡 首次运行会发送一次匿名安装统计（仅版本号 + OS）。`OPENINVEST_NO_TELEMETRY=1` / `DO_NOT_TRACK=1` 可关闭，详见 [docs/wiki/19-telemetry-and-analytics.md](../docs/wiki/19-telemetry-and-analytics.md)。

`install.sh` 用 symlink 把两个 skill 装到：
- `~/.claude/skills/invest/`
- `~/.claude/skills/invest-setup/`

skill 是 symlink，源目录里的 SKILL.md / scripts/ 更新即生效，不需要重装。
后端从 PyPI 分发（`run.sh` 内部走 `uvx openinvest`），更新跑 `run.sh update`。

## 设计原则：每个 skill 单一工作场景

按 OpenClaw `convex-setup-auth` / `openclaw-pr-maintainer` 等案例：

> **单一 skill 不混多个责任**——一次性 onboarding 跟日常使用拆开，因为 agent
> 每次启动都会把 SKILL.md description 加载进 context，混在一起 token 浪费 + 触发
> 词不清晰。

之前 `skill/SKILL.md` 209 行混了 4 个责任（onboarding / 日常 / API / 故障排查），
现在拆成 invest + invest-setup 两个 skill，frontmatter description 含明确的
**When to Use / When NOT to Use** 段，agent 自动按场景选 skill。

## 跨 agent 兼容

SKILL.md 是 [agentskills.io](https://agentskills.io) 开放标准，理论上兼容
Claude Code / Cursor / OpenCode / OpenHands / Cline / Goose / Hermes-Agent /
OpenClaw / Gemini CLI / Codex 等 35+ 客户端。`install.sh` 默认装到 Claude Code
路径，其他客户端用 `CLAUDE_SKILLS_DIR=<path> bash skills/install.sh` 覆盖。

## 也读这些

- 各 skill 自己的 README:
  - [`invest/README.md`](invest/README.md)
  - [`invest-setup/README.md`](invest-setup/README.md)
- 项目架构 wiki：[../docs/wiki/](../docs/wiki/)
