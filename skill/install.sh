#!/bin/bash
# 把仓库里的 skill/SKILL.md + skill/run.sh 通过 symlink 装到 ~/.claude/skills/invest/。
# 这样以后改 skill 协议（SKILL.md / run.sh）只需要在仓库里改、commit、pull 就生效，
# 不用每次手动两边同步。
#
# 用法:
#   cd $INVEST_HOME && bash skill/install.sh
#
# 幂等：再次运行会刷新 symlink 指向（如果之前是普通文件会先备份成 .bak.<ts>）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="${SKILL_DIR:-$HOME/.claude/skills/invest}"

echo "📦 Installing invest skill"
echo "   repo:        $REPO_ROOT"
echo "   skill dir:   $SKILL_DIR"

mkdir -p "$SKILL_DIR"

link_one() {
    local name="$1"
    local target="$REPO_ROOT/skill/$name"
    local link="$SKILL_DIR/$name"

    if [ ! -e "$target" ]; then
        echo "❌ missing in repo: $target" >&2
        exit 1
    fi

    # 如果链接已经指向同一个文件，跳过
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
        echo "   ✅ $name (already linked)"
        return
    fi

    # 已存在但不是预期的 symlink → 先备份再覆盖
    if [ -e "$link" ] || [ -L "$link" ]; then
        local backup="$link.bak.$(date +%Y%m%d_%H%M%S)"
        mv "$link" "$backup"
        echo "   📦 backed up old $name → $backup"
    fi

    ln -s "$target" "$link"
    echo "   🔗 linked $name → $target"
}

link_one SKILL.md
link_one run.sh

echo
echo "✅ Done. Restart Claude Code if it was running."
echo "   Test with: $SKILL_DIR/run.sh status"
