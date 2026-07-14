"""第二意见 CLI（Gemini/agy）探测 + 调用契约

agy 优先于 gemini（agy 用位置参数传 prompt，gemini 走 stdin——两者调用方式
不同，混用会报 "empty prompt"）；单参数受 Linux MAX_ARG_STRLEN=128KB 硬限，
超长 prompt 需防御性截断。
跑：uv run pytest tests/test_second_opinion_cli.py -q
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import openinvest.jobs.daily_report as daily_report_mod


def _fake_which(paths: dict):
    return lambda name: paths.get(name)


def test_prefers_agy_over_gemini(monkeypatch):
    monkeypatch.setattr(
        daily_report_mod.shutil, "which",
        _fake_which({"agy": "/usr/bin/agy", "gemini": "/usr/bin/gemini"}),
    )
    monkeypatch.delenv("INVEST_SECOND_OPINION_CLI", raising=False)
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return MagicMock(returncode=0, stdout="agree\n", stderr="")

    monkeypatch.setattr(daily_report_mod.subprocess, "run", fake_run)

    out = daily_report_mod._run_gemini_cli_review("hi")

    assert out == "agree"
    assert captured["args"] == ["/usr/bin/agy", "-p", "hi"]
    assert "input" not in captured["kwargs"]  # agy 走位置参数,不是 stdin


def test_falls_back_to_gemini_when_agy_absent(monkeypatch):
    monkeypatch.setattr(
        daily_report_mod.shutil, "which",
        _fake_which({"gemini": "/usr/bin/gemini"}),
    )
    monkeypatch.delenv("INVEST_SECOND_OPINION_CLI", raising=False)
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(daily_report_mod.subprocess, "run", fake_run)

    out = daily_report_mod._run_gemini_cli_review("hi")

    assert out == "ok"
    assert captured["args"] == ["/usr/bin/gemini"]
    assert captured["kwargs"]["input"] == "hi"  # gemini 走 stdin


def test_no_cli_available_is_explicit_skip(monkeypatch):
    monkeypatch.setattr(daily_report_mod.shutil, "which", _fake_which({}))
    monkeypatch.delenv("INVEST_SECOND_OPINION_CLI", raising=False)

    out = daily_report_mod._run_gemini_cli_review("hi")

    assert out.startswith("Skipped:")
    assert "agy" in out and "gemini" in out


def test_env_override_forces_specific_cli(monkeypatch):
    monkeypatch.setattr(
        daily_report_mod.shutil, "which",
        _fake_which({"agy": "/usr/bin/agy", "my-cli": "/opt/my-cli"}),
    )
    monkeypatch.setenv("INVEST_SECOND_OPINION_CLI", "my-cli")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return MagicMock(returncode=0, stdout="x\n", stderr="")

    monkeypatch.setattr(daily_report_mod.subprocess, "run", fake_run)

    daily_report_mod._run_gemini_cli_review("hi")

    # 走 agy 分支(非 gemini 名字都当位置参数调用)
    assert captured["args"] == ["/opt/my-cli", "-p", "hi"]


def test_long_prompt_truncated_before_exec(monkeypatch):
    monkeypatch.setattr(
        daily_report_mod.shutil, "which",
        _fake_which({"agy": "/usr/bin/agy"}),
    )
    monkeypatch.delenv("INVEST_SECOND_OPINION_CLI", raising=False)
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return MagicMock(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(daily_report_mod.subprocess, "run", fake_run)

    huge = "x" * 200_000
    daily_report_mod._run_gemini_cli_review(huge)

    sent_prompt = captured["args"][2]
    assert len(sent_prompt.encode("utf-8")) < 128_000  # 不顶到内核 MAX_ARG_STRLEN
    assert "截断" in sent_prompt


def test_nonzero_exit_is_error_not_exception(monkeypatch):
    monkeypatch.setattr(
        daily_report_mod.shutil, "which",
        _fake_which({"agy": "/usr/bin/agy"}),
    )
    monkeypatch.delenv("INVEST_SECOND_OPINION_CLI", raising=False)
    monkeypatch.setattr(
        daily_report_mod.subprocess, "run",
        lambda *a, **k: MagicMock(returncode=1, stdout="", stderr="auth failed"),
    )

    out = daily_report_mod._run_gemini_cli_review("hi")

    assert out == "Error: auth failed"
