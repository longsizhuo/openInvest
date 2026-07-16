#!/usr/bin/env bash
# 发布 OpenClaw bundle-plugin 到 ClawHub（clawhub package publish）。
#
# 版本单一可信源 = plugin/skills/invest/SKILL.md frontmatter 的 version:
# （release-please 管理）。本脚本发布前把它盖章进 plugin/openclaw.plugin.json
# 和 plugin/package.json——这两个 JSON 里的 version 是"informational"，
# 平时不需要手动同步，发布时自动对齐。
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

exec clawhub package publish plugin \
  --family bundle-plugin \
  --name openinvest \
  --display-name "openInvest" \
  --version "$VERSION" \
  --source-repo longsizhuo/openInvest \
  --source-commit "$COMMIT" \
  --source-path plugin \
  "$@"
