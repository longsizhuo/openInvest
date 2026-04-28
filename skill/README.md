# Claude Code Skill 资源（纳入版本管理版）

这个目录把 Claude Code 的 invest skill 文件放进仓库，方便：

1. SKILL.md 里描述的 Coordinator-Worker 协议、subcommand 行为、prompts 字段等可以
   跟 `core/`、`agents/`、`scripts/skill.py` 一起在 PR 里联合 review。
2. 改 skill 协议（比如 `prepare_committee` 字段名、`save_committee` 持久化路径）
   不用人工同步本地 `~/.claude/skills/invest/`。
3. 任何人 clone 仓库后跑一行 install 就能装好 skill，不需要手动复制。

## 目录布局

```
skill/
├── SKILL.md       ← Coordinator-Worker 协议指南（Claude 加载时读这个）
├── run.sh         ← bootstrap + 子命令分发器（克隆仓库 / uv sync / exec scripts.skill）
├── install.sh     ← 把上面两个 symlink 到 ~/.claude/skills/invest/
└── README.md      ← 你正在看
```

## 使用

```bash
cd $INVEST_HOME           # 默认 ~/projects-review/invest
bash skill/install.sh
```

`install.sh` 做：

- 创建 `~/.claude/skills/invest/`（不存在的话）
- 在那里建立两条 symlink：
  - `~/.claude/skills/invest/SKILL.md` → `$INVEST_HOME/skill/SKILL.md`
  - `~/.claude/skills/invest/run.sh`   → `$INVEST_HOME/skill/run.sh`
- 已经存在的非 symlink 文件会先备份为 `<name>.bak.<timestamp>`，不会丢

幂等：重复运行只会刷新 symlink 指向。

## 修改协议时的工作流

```bash
cd $INVEST_HOME
# 1. 改 skill/SKILL.md 或 skill/run.sh
vim skill/SKILL.md
# 2. 测试（symlink 已生效，不需要重装）
~/.claude/skills/invest/run.sh status
# 3. commit + push 进仓库
git add skill/ && git commit -m "..." && git push
# 其他设备 git pull 之后立刻同步生效（symlink 不变）
```

## 自定义路径

```bash
SKILL_DIR=/some/other/path bash skill/install.sh
```

通常没必要——Claude Code 默认从 `~/.claude/skills/<name>/` 读 skill。

## 卸载

```bash
rm ~/.claude/skills/invest/SKILL.md ~/.claude/skills/invest/run.sh
# 如果只剩 .bak 备份，可手动恢复或 rm -rf 整个目录
```

仓库本身不会被影响。
