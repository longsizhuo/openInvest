#!/usr/bin/env bash
# 发布 openInvest 到 ClawHub——两个条目一次发齐：
#   1. bundle-plugin（`clawhub package publish plugin`）：OpenClaw 用户
#      `openclaw plugins install clawhub:openinvest` 装的东西（3 skills + MCP 声明）
#   2. standalone skill（`clawhub skill publish`）：ClawHub 目录里单独的 skill 条目，
#      发布前把 SKILL.md 的中文 description 换成下方固化的英文版（ClawHub 目录
#      面向英文用户；repo 内 SKILL.md 保持中文不动）
#
# 版本单一可信源 = plugin/skills/invest/SKILL.md frontmatter 的 version:
# （release-please 管理）。幂等：远端 latest 已是该版本的条目自动跳过——CI 重跑安全。
#
# 用法：
#   scripts/publish_clawhub.sh --dry-run   # 预览（会先真实盖章版本号）
#   scripts/publish_clawhub.sh             # 真实发布
#
# 前置：clawhub login 已完成；HEAD 已 push（溯源 commit 必须在 GitHub 上真实存在）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

command -v clawhub >/dev/null || { echo "缺 clawhub CLI：npm i -g clawhub" >&2; exit 1; }
command -v jq >/dev/null || { echo "缺 jq" >&2; exit 1; }

VERSION=$(grep -m1 '^version:' plugin/skills/invest/SKILL.md | sed 's/version:[[:space:]]*//; s/[[:space:]]*#.*//')
[ -n "$VERSION" ] || { echo "从 SKILL.md 读不到 version" >&2; exit 1; }

# 盖章两个 JSON（幂等：已一致则无 diff）
for f in plugin/openclaw.plugin.json plugin/package.json; do
  jq --arg v "$VERSION" '.version = $v' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
echo "版本盖章: $VERSION → plugin/openclaw.plugin.json, plugin/package.json"

# porcelain 同时覆盖 modified + untracked（git diff 看不见 untracked——
# 新文件没 commit 时溯源 commit 里根本不含它们，必须拦）
if [ -n "$(git status --porcelain -- plugin/openclaw.plugin.json plugin/package.json)" ]; then
  echo "⚠️ 两个 manifest 有未提交状态——先 commit + push 再发布（溯源 commit 必须包含它们）" >&2
  git status --short -- plugin/openclaw.plugin.json plugin/package.json >&2
  exit 1
fi

COMMIT=$(git rev-parse HEAD)
if ! git merge-base --is-ancestor "$COMMIT" "$(git rev-parse origin/main 2>/dev/null || echo "$COMMIT")"; then
  echo "⚠️ HEAD 未 push 到 origin/main——溯源 commit 必须在 GitHub 上存在" >&2
  exit 1
fi

_remote_latest() {  # $1 = skill|package
  if [ "$1" = skill ]; then
    clawhub inspect openinvest --json 2>/dev/null | jq -r '.skill.tags.latest // empty'
  else
    clawhub package inspect openinvest --json 2>/dev/null | jq -r '.package.latestVersion // .latestVersion // empty'
  fi
}

# ---- 1. bundle-plugin ----
if [ "$(_remote_latest package)" = "$VERSION" ] && [[ " $* " != *" --dry-run "* ]]; then
  echo "⏭  bundle-plugin openinvest@$VERSION 已在远端，跳过"
else
  clawhub package publish plugin \
    --family bundle-plugin \
    --name openinvest \
    --display-name "openInvest" \
    --version "$VERSION" \
    --source-repo longsizhuo/openInvest \
    --source-commit "$COMMIT" \
    --source-path plugin \
    "$@"
fi

# ---- 2. standalone skill（英文 description 变体）----
# ClawHub 目录面向英文用户；repo 内 SKILL.md description 是中文（Claude/Hermes
# 等宿主 agent 的双语触发场景需要）。这里换 description 一行，其余原样。
# 注意：英文文案里不能出现 ASCII 冒号+空格（YAML 会解析成新 key），用破折号。
DESC_EN='openInvest multi-asset AI investment committee — **daily use**. Read portfolio / live prices / strategy / decision history / adjust positions / run a 4-role LLM committee for an investment verdict. Supports any yfinance symbol (A-share / HK / US / ETF / crypto / commodities) and any currency. **Two paths** — (1) Coordinator, Claude Code spawns 4 subagents, saves DeepSeek tokens; (2) Direct, any agent (Codex / Hermes / OpenClaw / Cursor / Cline / plain script) runs `run.sh run_committee <SYM>` for a one-shot verdict. **Trigger scenarios** — "show portfolio", "how is my P&L", "should I buy/sell X", "analyze X", "run committee on X", "track AAPL", "add/trim a position, log a trade". **First-time install uses a separate skill `invest-setup`** (switch to it when `doctor` returns `needs_setup`). Backend — longsizhuo/openInvest.'

if [ "$(_remote_latest skill)" = "$VERSION" ] && [[ " $* " != *" --dry-run "* ]]; then
  echo "⏭  skill openinvest@$VERSION 已在远端，跳过"
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  cp -rL plugin/skills/invest "$TMP/invest-en"
  DESC_EN="$DESC_EN" python3 - "$TMP/invest-en/SKILL.md" <<'PYEOF'
import os, re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
new, n = re.subn(r"^description:.*$", "description: " + os.environ["DESC_EN"],
                 text, count=1, flags=re.MULTILINE)
assert n == 1, "SKILL.md 里没找到 description 行"
open(path, "w", encoding="utf-8").write(new)
PYEOF
  clawhub skill publish "$TMP/invest-en" \
    --slug openinvest \
    --name "openInvest" \
    --version "$VERSION" \
    --source-repo longsizhuo/openInvest \
    --source-commit "$COMMIT" \
    --source-path plugin/skills/invest \
    "$@"
fi

echo "✅ ClawHub 发布检查完毕（skill + bundle-plugin @ $VERSION）"
