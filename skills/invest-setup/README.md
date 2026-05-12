# invest-setup skill — 首次 onboarding

参考 [OpenClaw `convex-setup-auth`](https://github.com/openclaw/clawhub/tree/main/.agents/skills/convex-setup-auth)
模式 —— **首次安装/onboarding 独立成一个 skill**，跟日常使用的
[`../invest/`](../invest/) skill 拆开。

## 为什么拆

之前单一 `skill/SKILL.md` 209 行混了 4 个责任：onboarding（一次性）+ 日常使用
（高频）+ Web API 端点（中频）+ 出错处理（偶发）。Agent 每次启动都吃这么多 token。

按 OpenClaw 实际做法：**单一 skill = 单一工作场景**。所以 setup 拆出来，agent
在 `doctor` 返回 `needs_setup` 时才会加载 invest-setup，否则只加载 invest。

详细 ADR 见 [`docs/wiki/11-rl-training.md`](../../docs/wiki/11-rl-training.md)
"Prompt 组织"段。

## 目录布局

```
skills/invest-setup/
├── SKILL.md                    ← invest-setup 主入口，含 frontmatter trigger
├── scripts/
│   └── run.sh                  ← symlink → ../../invest/scripts/run.sh （reuse）
├── references/
│   └── onboarding-detailed.md  ← symlink → ../../invest/references/onboarding.md
└── README.md                   ← 你正在看
```

scripts/ + references/ 用 symlink 复用 invest skill 的实现，避免重复维护。

## 装到 ~/.claude/skills/

由 `../install.sh` 一次装两个 skill：

```bash
cd $INVEST_HOME && bash skills/install.sh
```

装完后：
- `~/.claude/skills/invest/` ← `skills/invest/`
- `~/.claude/skills/invest-setup/` ← `skills/invest-setup/`

Agent 加载时按 frontmatter 的 description 触发词决定走哪个 skill：
- "帮我初始化 invest" / "set up invest" / `doctor` 返回 `needs_setup` → invest-setup
- "看持仓" / "跑委员会" / "分析 X" → invest

## When to Use / When NOT to Use

SKILL.md frontmatter 已经写了明确条件，agent 不会乱混。简单总结：

| | Use invest-setup | Use invest |
|---|---|---|
| 首次跑 | ✅ | ❌ |
| memory / user_profile 缺失 | ✅ | ❌ |
| 用户说"reset / 重新配置" | ✅ | ❌ |
| 已 onboard，看持仓 | ❌ | ✅ |
| 跑委员会决策 | ❌ | ✅ |
| 加减仓 / 改账本 | ❌ | ✅ |
