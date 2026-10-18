"""plugin run.sh：只在后端一个字都没输出就失败时才追加"检查网络"提示（2026-10）

业务错误（CLI 自己打印了 JSON 原因）再追加"首次运行需网络 / 先 uv sync"会把 agent 带去
重装依赖、查网络。用一个假的 uvx 模拟三种情况：业务报错、拉包失败、正常输出（含 stdin 透传）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

RUN_SH = Path(__file__).resolve().parents[1] / "plugin/skills/invest/scripts/run.sh"

FAKE_UVX = """#!/bin/bash
case "$FAKE_MODE" in
  business) echo '{"status":"error","error":"缺少 --max-single"}'; exit 1 ;;
  fetch)    echo "error: Failed to fetch openinvest from PyPI" >&2; exit 2 ;;
  stdin)    cat ;;
  *)        echo '{"status":"ok"}' ;;
esac
"""


def _run(tmp_path, mode, stdin=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uvx = bin_dir / "uvx"
    uvx.write_text(FAKE_UVX)
    uvx.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_MODE": mode,
           "INVEST_HOME": str(tmp_path / "home"), "OPENINVEST_DEV_MODE": "0"}
    p = subprocess.run(["bash", str(RUN_SH), "status"], input=stdin, env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def test_business_error_is_passed_through_without_network_hint(tmp_path):
    rc, lines = _run(tmp_path, "business")
    assert rc == 1
    assert lines == [{"status": "error", "error": "缺少 --max-single"}]


def test_silent_failure_gets_structured_hint(tmp_path):
    rc, lines = _run(tmp_path, "fetch")
    assert rc == 2
    assert len(lines) == 1 and lines[0]["status"] == "error"
    assert "stderr" in lines[0]["hint"] and "PyPI" in lines[0]["hint"]


def test_success_and_stdin_pass_through(tmp_path):
    assert _run(tmp_path, "ok") == (0, [{"status": "ok"}])
    assert _run(tmp_path, "stdin", stdin='{"profile": 1}\n') == (0, [{"profile": 1}])


def test_setup_skill_run_sh_is_symlink_to_invest():
    """invest-setup 的 run.sh 必须是指向 invest 那份的 symlink，不是第二份拷贝（#236）：
    两份 bootstrap 一旦分叉，SPEC 版本 / uvx 兜底 / 提示逻辑就会各改各的。"""
    setup = RUN_SH.parents[2] / "invest-setup/scripts/run.sh"
    assert setup.is_symlink()
    assert setup.resolve() == RUN_SH.resolve()
