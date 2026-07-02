#!/bin/bash
# 把仓库 skills/ 下两个 skill 通过 symlink 装到 ~/.claude/skills/。
# 改 skill 协议（SKILL.md / scripts/ / references/）只需在仓库里改 + commit + pull
# 即生效，不用每次手动两边同步。
#
# 用法:
#   cd $INVEST_HOME && bash skills/install.sh
#
# 幂等：再次运行会刷新 symlink 指向。
#
# 结构（参考 OpenClaw convex-setup-auth 模式，setup 和日常使用拆开）:
#   ~/.claude/skills/invest/          (日常使用：portfolio / committee / 买卖)
#   │   ├── SKILL.md      → repo skills/invest/SKILL.md
#   │   ├── scripts/      → repo skills/invest/scripts/
#   │   └── references/   → repo skills/invest/references/
#   └── invest-setup/                 (一次性 onboarding)
#       ├── SKILL.md      → repo skills/invest-setup/SKILL.md
#       ├── scripts/      → repo skills/invest-setup/scripts/
#       └── references/   → repo skills/invest-setup/references/

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAUDE_SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

echo "📦 Installing openInvest skills (日常 + setup)"
echo "   repo:                $REPO_ROOT"
echo "   claude skills dir:   $CLAUDE_SKILLS_DIR"

link_one() {
    # symlink a single file or directory from repo to ~/.claude/skills/<dst>/<name>
    local repo_dir="$1"   # 仓库内目录（skills/invest / skills/invest-setup）
    local dst="$2"        # ~/.claude/skills/<name>/
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

# 兼容老布局：以前 symlink 指向旧 /skill/（单数）路径，发现就清掉重链
for old in "$INVEST_DIR/SKILL.md" "$INVEST_DIR/scripts" "$INVEST_DIR/references" "$INVEST_DIR/run.sh"; do
    if [ -L "$old" ]; then
        old_target=$(readlink "$old")
        # 旧 target 含 /skill/ (单数旧路径)
        if echo "$old_target" | grep -q "/invest/skill/"; then
            backup="$old.legacy.$(date +%Y%m%d_%H%M%S)"
            mv "$old" "$backup"
            echo "   📦 detected legacy /skill/ symlink → backed up to $backup"
        fi
    fi
done

echo
echo "[1/3] invest (日常使用 skill)"
link_one skills/invest "$INVEST_DIR" SKILL.md
link_one skills/invest "$INVEST_DIR" scripts
link_one skills/invest "$INVEST_DIR" references

# ---- 副 skill: ~/.claude/skills/invest-setup/  (一次性 onboarding) ----
SETUP_DIR="$CLAUDE_SKILLS_DIR/invest-setup"
mkdir -p "$SETUP_DIR"

# 同样清理老 /skill-setup/ symlink
for old in "$SETUP_DIR/SKILL.md" "$SETUP_DIR/scripts" "$SETUP_DIR/references"; do
    if [ -L "$old" ]; then
        old_target=$(readlink "$old")
        if echo "$old_target" | grep -q "/invest/skill-setup/"; then
            backup="$old.legacy.$(date +%Y%m%d_%H%M%S)"
            mv "$old" "$backup"
            echo "   📦 detected legacy /skill-setup/ symlink → backed up to $backup"
        fi
    fi
done

echo
echo "[2/3] invest-setup (一次性 onboarding skill)"
link_one skills/invest-setup "$SETUP_DIR" SKILL.md
link_one skills/invest-setup "$SETUP_DIR" scripts
link_one skills/invest-setup "$SETUP_DIR" references

# ---- skill: ~/.claude/skills/okf-frontmatter/  (文档维护 + 查找) ----
OKF_DIR="$CLAUDE_SKILLS_DIR/okf-frontmatter"
mkdir -p "$OKF_DIR"

echo
echo "[3/3] okf-frontmatter (OKF 文档维护 + 查找 skill)"
link_one skills/okf-frontmatter "$OKF_DIR" SKILL.md
link_one skills/okf-frontmatter "$OKF_DIR" scripts
link_one skills/okf-frontmatter "$OKF_DIR" references

# ---- 匿名安装统计（仅版本号 + OS，不含任何 key/持仓/个人信息）----
# 设 OPENINVEST_NO_TELEMETRY=1 可跳过
if [ "${OPENINVEST_NO_TELEMETRY:-}" != "1" ]; then
    _oi_ver=$(grep -m 1 '^version = ' "$REPO_ROOT/pyproject.toml" 2>/dev/null | cut -d '"' -f 2 || echo "unknown")
    _oi_os=$(uname -s 2>/dev/null || echo "unknown")
    curl -sf --max-time 3 \
      "https://umami.involutionhell.com/api/send" \
      -H "Content-Type: application/json" \
      -H "User-Agent: openInvest-install/${_oi_ver}" \
      -d "{\"type\":\"event\",\"payload\":{\"hostname\":\"openinvest-install\",\"language\":\"en\",\"url\":\"/install\",\"website\":\"0373eb82-72c8-4f5f-864c-11f9e7c997fb\",\"name\":\"install\",\"data\":{\"version\":\"${_oi_ver}\",\"os\":\"${_oi_os}\"}}}" \
      >/dev/null 2>&1 || true
fi

echo
echo "✅ Done. Restart Claude Code if it was running."
echo
echo "Daily usage:      $INVEST_DIR/scripts/run.sh doctor"
echo "First-time setup: agent 自动加载 invest-setup skill 当 doctor 返回 needs_setup"
