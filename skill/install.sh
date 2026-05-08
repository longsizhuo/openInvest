#!/bin/bash
# 把仓库里的 skill/ 通过 symlink 装到 ~/.claude/skills/invest/。
# 改 skill 协议（SKILL.md / scripts/ / references/）只需在仓库里改 + commit + pull
# 即生效，不用每次手动两边同步。
#
# 用法:
#   cd $INVEST_HOME && bash skill/install.sh
#
# 幂等：再次运行会刷新 symlink 指向（如果之前是普通文件会先备份成 .bak.<ts>）。
#
# 结构（agent-skills 标准布局，参考 https://agentskills.io/skill-creation）:
#   ~/.claude/skills/invest/
#   ├── SKILL.md          → repo skill/SKILL.md         (level 2: 决策树，always loaded)
#   ├── scripts/          → repo skill/scripts/         (level 3: 可执行入口)
#   │   └── run.sh
#   └── references/       → repo skill/references/      (level 3: 详细操作手册)
#       └── *.md

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="${SKILL_DIR:-$HOME/.claude/skills/invest}"

echo "📦 Installing invest skill"
echo "   repo:        $REPO_ROOT"
echo "   skill dir:   $SKILL_DIR"

mkdir -p "$SKILL_DIR"

link_one() {
    # symlink a single file or directory from repo skill/ → ~/.claude/skills/invest/
    local name="$1"
    local target="$REPO_ROOT/skill/$name"
    local link="$SKILL_DIR/$name"

    if [ ! -e "$target" ]; then
        echo "❌ missing in repo: $target" >&2
        exit 1
    fi

    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
        echo "   ✅ $name (already linked)"
        return
    fi

    if [ -e "$link" ] || [ -L "$link" ]; then
        local backup="$link.bak.$(date +%Y%m%d_%H%M%S)"
        mv "$link" "$backup"
        echo "   📦 backed up old $name → $backup"
    fi

    ln -s "$target" "$link"
    echo "   🔗 linked $name → $target"
}

# 旧布局清理：以前 run.sh 直接在 SKILL_DIR 顶层。新版本移到 scripts/run.sh。
# 如果发现旧的顶层 run.sh symlink，先备份。
if [ -L "$SKILL_DIR/run.sh" ] || [ -f "$SKILL_DIR/run.sh" ]; then
    backup="$SKILL_DIR/run.sh.legacy.$(date +%Y%m%d_%H%M%S)"
    mv "$SKILL_DIR/run.sh" "$backup"
    echo "   📦 detected legacy top-level run.sh → backed up to $backup"
    echo "      新调用路径: $SKILL_DIR/scripts/run.sh"
fi

link_one SKILL.md
link_one scripts
link_one references

echo
echo "✅ Done. Restart Claude Code if it was running."
echo "   Test with: $SKILL_DIR/scripts/run.sh doctor"
