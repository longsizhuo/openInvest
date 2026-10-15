"""数据目录还没 init 时，CLI 写命令给结构化 JSON 而不是 traceback（2026-10）

traceback 时 stdout 为空，plugin run.sh 会把它当"拉包失败"，提示 agent 去查网络 / uv sync。
"""
from __future__ import annotations

import json
import sys

import pytest

from openinvest import cli


def test_buy_on_uninitialized_home_prints_json_error(monkeypatch, capfd):
    monkeypatch.delenv("INVEST_API_BASE", raising=False)
    monkeypatch.setattr(sys, "argv", ["openinvest", "buy", "--symbol", "X", "--units", "1", "--price", "1"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "error" and "init" in out["error"] and "doctor" in out["hint"]
