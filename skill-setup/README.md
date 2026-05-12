# invest-setup skill

参考 [OpenClaw `convex-setup-auth`](https://github.com/openclaw/clawhub/blob/main/.agents/skills/convex-setup-auth/SKILL.md)
模式 —— **首次安装/onboarding 独立成一个 skill**，跟日常使用的 `invest` skill 拆开。

## 为什么拆

之前 `skill/SKILL.md` 209 行混了 4 个责任：onboarding（一次性）+ 日常使用（高频）
+ Web API 端点（中频）+ 出错处理（偶发）。Agent 每次启动都吃这么多 token。

按 OpenClaw 实际做法（参考 wiki 11 章节 "Prompt 组织"）：单一 skill = 单一工作场景。
所以 setup 拆出来，agent 在 `doctor` 返回 `needs_setup` 时才会加载 invest-setup，
否则只加载 invest。

## 目录布局

```
skill-setup/
├── SKILL.md                    ← invest-setup 主入口，含 frontmatter trigger
├── scripts/run.sh              ← symlink → ../skill/scripts/run.sh （reuse）
├── references/
│   └── onboarding-detailed.md  ← symlink → ../skill/references/onboarding.md
└── README.md                   ← 你正在看
```

scripts/ + references/ 都用 symlink 复用 `skill/` 的实现，避免重复维护。

## 装到 ~/.claude/skills/

`skill/install.sh` 一次装两个 skill：
- `~/.claude/skills/invest/` ← `skill/`
- `~/.claude/skills/invest-setup/` ← `skill-setup/`

Agent 加载时按 frontmatter 的 description 触发词决定走哪个 skill。
