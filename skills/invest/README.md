# invest skill — 日常使用

openInvest 的**日常使用 agent skill**。看持仓 / 跑委员会 / 加减仓 / btw 关联分析。

> **首次安装**走另一个 skill: [`../invest-setup/`](../invest-setup/) —— agent 看到
> `doctor` 返回 `needs_setup` 时会自动加载它走 5 问 onboarding，完了才轮到本 skill。

## 目录布局

```
skills/invest/
├── SKILL.md          ← agent 触发指引（决策树 / 子命令 / Web API 端点）
├── scripts/
│   └── run.sh        ← bootstrap + 子命令分发器（首次跑自动 git clone + uv sync）
├── references/
│   ├── committee-protocol.md     ← Coordinator 路径详细 stage
│   ├── two-paths.md              ← Coordinator vs Direct 区别
│   ├── adding-assets.md          ← 加新追踪 symbol
│   ├── troubleshooting.md        ← doctor 全绿但还出错时看
│   └── onboarding.md             ← 详细 5 问流程（invest-setup skill 也引用这个）
└── README.md         ← 你在看
```

## 安装

被父目录 `../install.sh` 一次装两个 skill：

```bash
cd $INVEST_HOME              # 默认 ~/projects-review/invest
bash skills/install.sh        # 同时装 invest + invest-setup
```

`install.sh` 装到 `~/.claude/skills/invest/` 和 `~/.claude/skills/invest-setup/`，
内容都是指向本仓库的 symlink —— 改 `SKILL.md` / `scripts/` 后 `git pull` 即生效，
不需要重装。

## 修改协议时的工作流

```bash
cd $INVEST_HOME
# 1. 改 SKILL.md / scripts/run.sh / references/*.md
vim skills/invest/SKILL.md
# 2. 测试（symlink 已生效，不需要重装）
~/.claude/skills/invest/scripts/run.sh status
# 3. commit + push
git add skills/invest/ && git commit -m "..." && git push
# 4. 其他设备 git pull 后立即同步生效（symlink 不变）
#    生产服务器有 invest-deploy.timer 每小时自动 git pull
```

## 跟 invest-setup skill 的关系

| | invest（本 skill） | invest-setup |
|---|---|---|
| 何时触发 | 日常—— "看持仓"、"分析 X"、"加仓" | 首次—— "帮我初始化 invest"、`doctor` 返回 `needs_setup` |
| 频率 | 高频持续使用 | 一次（onboard 完就退场） |
| 内部 scripts | 独立 `scripts/run.sh` | symlink → `../invest/scripts/run.sh`（reuse） |
| 内部 references | 独立 5 个 md | `onboarding-detailed.md` symlink → `../invest/references/onboarding.md` |

设计参考 OpenClaw 的 [`convex-setup-auth`](https://github.com/openclaw/clawhub/tree/main/.agents/skills/convex-setup-auth)
模式：**单一 skill 对应单一工作场景**。

## 自定义安装路径

```bash
CLAUDE_SKILLS_DIR=/some/other/path bash skills/install.sh
```

通常没必要——Claude Code 默认从 `~/.claude/skills/<name>/` 读 skill。

## 卸载

```bash
rm -rf ~/.claude/skills/invest ~/.claude/skills/invest-setup
```

仓库本身不会被影响。

## 跨 agent 兼容性

SKILL.md 是 [agentskills.io](https://agentskills.io) 开放标准，理论上兼容
Claude Code / Cursor / OpenCode / OpenHands / Cline / Goose / Gemini CLI / Codex
等 35+ agent 客户端。但**install.sh 写死了 `~/.claude/skills/`**（Claude Code 路径），
其他客户端可能用 `~/.cursor/skills/` 等不同位置——fork 用户自己改 `CLAUDE_SKILLS_DIR`
环境变量。

OpenClaw 用户可以走 `clawhub install`（如果将来发布到 ClawHub registry）。
当前最低公分母 = `git clone + bash skills/install.sh`。

## 也读这些

- 完整 SKILL.md 协议见 [`SKILL.md`](SKILL.md)
- 项目架构 wiki：[github.com/longsizhuo/openInvest/tree/main/docs/wiki](https://github.com/longsizhuo/openInvest/tree/main/docs/wiki)
- 双路径决策：[`references/two-paths.md`](references/two-paths.md)
