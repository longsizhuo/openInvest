#!/bin/bash
# 把仓库里的 skill/ + skill-setup/ 通过 symlink 装到 ~/.claude/skills/。
# 改 skill 协议（SKILL.md / scripts/ / references/）只需在仓库里改 + commit + pull
# 即生效，不用每次手动两边同步。
#
# 用法:
#   cd $INVEST_HOME && bash skill/install.sh
#
# 幂等：再次运行会刷新 symlink 指向。
#
# 结构（按 OpenClaw convex-setup-auth 模式，setup 和日常使用拆开）:
#   ~/.claude/skills/invest/          (日常使用：portfolio / committee / 买卖)
#   │   ├── SKILL.md      → repo skill/SKILL.md
#   │   ├── scripts/      → repo skill/scripts/
#   │   └── references/   → repo skill/references/
#   └── invest-setup/                 (一次性 onboarding)
#       ├── SKILL.md      → repo skill-setup/SKILL.md
#       ├── scripts/      → repo skill-setup/scripts/
#       └── references/   → repo skill-setup/references/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

echo "📦 Installing invest skills (日常 + setup)"
echo "   repo:                $REPO_ROOT"
echo "   claude skills dir:   $CLAUDE_SKILLS_DIR"

link_one() {
    # symlink a single file or directory from repo to ~/.claude/skills/<dst>/<name>
    local repo_dir="$1"   # 仓库内目录（skill / skill-setup）
    local dst="$2"        # ~/.claude/skills/<dst>/
    local name="$3"       # SKILL.md / scripts / references
    local target="$REPO_ROOT/$repo_dir/$name"
    local link="$dst/$name"

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

# ---- 主 skill: ~/.claude/skills/invest/  (日常使用) ----
INVEST_DIR="$CLAUDE_SKILLS_DIR/invest"
mkdir -p "$INVEST_DIR"

# 旧布局清理：以前 run.sh 直接在 invest/ 顶层。新版本移到 scripts/run.sh。
if [ -L "$INVEST_DIR/run.sh" ] || [ -f "$INVEST_DIR/run.sh" ]; then
    backup="$INVEST_DIR/run.sh.legacy.$(date +%Y%m%d_%H%M%S)"
    mv "$INVEST_DIR/run.sh" "$backup"
    echo "   📦 detected legacy top-level run.sh → backed up to $backup"
fi

echo
echo "[1/2] invest (日常使用 skill)"
link_one skill "$INVEST_DIR" SKILL.md
link_one skill "$INVEST_DIR" scripts
link_one skill "$INVEST_DIR" references

# ---- 副 skill: ~/.claude/skills/invest-setup/  (一次性 onboarding) ----
SETUP_DIR="$CLAUDE_SKILLS_DIR/invest-setup"
mkdir -p "$SETUP_DIR"

echo
echo "[2/2] invest-setup (一次性 onboarding skill)"
link_one skill-setup "$SETUP_DIR" SKILL.md
link_one skill-setup "$SETUP_DIR" scripts
link_one skill-setup "$SETUP_DIR" references

echo
echo "✅ Done. Restart Claude Code if it was running."
echo
echo "Daily usage:     $INVEST_DIR/scripts/run.sh doctor"
echo "First-time setup: agent 自动加载 invest-setup skill 当 doctor 返回 needs_setup"
