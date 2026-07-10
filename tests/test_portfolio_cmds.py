"""skill_cmds/portfolio_cmds CLI 动钱命令测试（issue #179 P2）。

此前零测试且缺 `import argparse`（被 from __future__ annotations 的懒 annotation
掩护着不炸）。用 fake PortfolioManager 锁住 CLI → PM 的调用形状 + 错误路径退出码，
不碰真实 memory/。
"""
from __future__ import annotations

import argparse
import json

import pytest

import openinvest.skill_cmds.portfolio_cmds as pc


class _FakePM:
    def __init__(self):
        self.calls = []

    def deposit_cash(self, currency, amount, source=None):
        self.calls.append(("deposit", currency, amount, source))
        return {"status": "ok", "currency": currency, "amount": amount}

    def withdraw_cash(self, currency, amount, source=None):
        self.calls.append(("withdraw", currency, amount, source))
        if amount > 100:
            raise ValueError("余额不足")
        return {"status": "ok"}


@pytest.fixture
def fake_pm(monkeypatch):
    pm = _FakePM()
    monkeypatch.setattr(pc, "_resolve_pm", lambda: pm)
    return pm


def test_cmd_deposit_calls_pm(fake_pm, capfd):
    # _print_json 写 sys.__stdout__（CLI 的防噪声机制），要 fd 级捕获（capfd）
    args = argparse.Namespace(currency="CNY", amount=500.0)
    pc.cmd_deposit(args)
    assert fake_pm.calls == [("deposit", "CNY", 500.0, "skill_cli")]
    out = json.loads(capfd.readouterr().out)
    assert out["status"] == "ok"


def test_cmd_withdraw_rejects_nonpositive(fake_pm, capsys):
    with pytest.raises(SystemExit) as ei:
        pc.cmd_withdraw(argparse.Namespace(currency="CNY", amount=0))
    assert ei.value.code == 1
    assert fake_pm.calls == [], "amount<=0 不得触达 PM"


def test_cmd_withdraw_insufficient_exits_with_json(fake_pm):
    """历史行为契约：余额不足 → SystemExit，携带 JSON 错误串。"""
    with pytest.raises(SystemExit) as ei:
        pc.cmd_withdraw(argparse.Namespace(currency="CNY", amount=999.0))
    payload = json.loads(str(ei.value))
    assert payload["status"] == "error"


def test_argparse_imported():
    """回归：portfolio_cmds 曾缺 import argparse，靠懒 annotation 侥幸不炸——
    谁若去掉 from __future__ import annotations 就当场 NameError。"""
    assert hasattr(pc, "argparse")
