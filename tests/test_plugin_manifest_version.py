"""插件 manifest 的 version 策略（#233-6）。

- Codex：version 挂在 invest-skill 版本线上，随 SKILL.md 同一 Release PR bump
  （release-please-config.json extra-files，路径前导 / = 仓库根相对）。Codex 启动/手动
  刷新按 version 比对决定是否重装缓存，不写 version 时恒为 "local"，这条路径永不刷新。
- Claude Code：故意不写 version。marketplace 里相对路径的插件不写 version 时按 commit
  SHA 计版本，每个 commit 都能推到用户；写死 version 反而把 invest-setup / invest-backup /
  .mcp.json 的改动冻在缓存里，直到 invest-skill 下次发版。
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = "plugin/skills/invest"
CODEX = "plugin/.codex-plugin/plugin.json"
CLAUDE = "plugin/.claude-plugin/plugin.json"


def _extra_files():
    cfg = json.loads((ROOT / "release-please-config.json").read_text())
    return {
        (f.get("type"), f.get("path"), f.get("jsonpath"))
        for f in cfg["packages"][COMPONENT]["extra-files"]
        if isinstance(f, dict)
    }


def test_codex_manifest_tracks_invest_skill_version():
    version = json.loads((ROOT / ".release-please-manifest.json").read_text())[COMPONENT]
    assert json.loads((ROOT / CODEX).read_text()).get("version") == version
    assert ("json", f"/{CODEX}", "$.version") in _extra_files()


def test_claude_manifest_stays_unversioned():
    assert "version" not in json.loads((ROOT / CLAUDE).read_text())
    assert not any(path == f"/{CLAUDE}" for _, path, _ in _extra_files())
