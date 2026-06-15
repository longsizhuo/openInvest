"""PnL snapshot 关键 helper 测试 — 时区判断 / 隐私脱敏 / 基准对齐。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import subprocess

from jobs.pnl_snapshot import (
    _auto_push_svg,
    _is_trading_window,
    _redact_token_in,
)


def _bj(year, month, day, hour, minute=0):
    """北京时间 helper"""
    return datetime(year, month, day, hour, minute,
                    tzinfo=timezone(timedelta(hours=8)))


# ---------- _is_trading_window 时区正确性（audit timezone bug 修复回归）----------

def test_is_trading_window_beijing_morning():
    assert _is_trading_window(_bj(2026, 4, 28, 10, 0)) is True


def test_is_trading_window_beijing_evening():
    assert _is_trading_window(_bj(2026, 4, 28, 22, 0)) is True


def test_is_trading_window_beijing_midnight():
    """凌晨 4 点是噪声窗口"""
    assert _is_trading_window(_bj(2026, 4, 28, 4, 0)) is False


def test_is_trading_window_weekend():
    """周六凌晨"""
    assert _is_trading_window(_bj(2026, 5, 2, 10, 0)) is False


def test_is_trading_window_utc_input():
    """关键 bug 修复：UTC 服务器跑时也按北京时间判断"""
    # UTC 20:00 = 北京 04:00 (凌晨)，应该 False
    utc_4am_bj = datetime(2026, 4, 28, 20, 0, tzinfo=timezone.utc)
    assert _is_trading_window(utc_4am_bj) is False

    # UTC 02:00 = 北京 10:00 (上午)，应该 True
    utc_10am_bj = datetime(2026, 4, 28, 2, 0, tzinfo=timezone.utc)
    assert _is_trading_window(utc_10am_bj) is True


# ---------- _redact_token_in（audit security M1）----------

def test_redact_token_in_url():
    sample = "fatal: unable to access 'https://x-access-token:gho_secretXYZ@github.com/foo/bar.git/'"
    out = _redact_token_in(sample)
    assert "gho_secretXYZ" not in out
    assert "x-access-token:***@" in out


def test_redact_does_not_break_clean_text():
    """没 token 的字符串原样返回"""
    sample = "everything fine"
    assert _redact_token_in(sample) == sample


def test_redact_handles_multiple_tokens():
    sample = (
        "https://x-access-token:tokenA@github.com/x/y "
        "https://x-access-token:tokenB@github.com/p/q"
    )
    out = _redact_token_in(sample)
    assert "tokenA" not in out
    assert "tokenB" not in out
    assert out.count("x-access-token:***@") == 2


# ---------- _auto_push_svg 失败路径不泄露 token（audit security M1 回归） ----------

_SECRET_TOKEN = "ghp_SUPERSECRET_should_never_leak"


def _fake_git_run_factory(push_stderr: str):
    """构造一个假的 subprocess.run，模拟 main 分支 push 失败时 git 的行为。

    push 失败 stderr 里带 authed_remote（含 token），用来验证返回的 reason 已脱敏。
    """
    def _fake_run(cmd, **kwargs):
        args = cmd[1:] if cmd and cmd[0] == "git" else cmd
        check = kwargs.get("check", False)

        def _done(returncode=0, stdout="", stderr=""):
            cp = subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
            if check and returncode != 0:
                raise subprocess.CalledProcessError(returncode, cmd, stdout, stderr)
            return cp

        if args[:2] == ["config", "--get"]:
            return _done(stdout="https://github.com/owner/repo.git\n")
        if args[:1] == ["diff"]:
            # diff --cached --quiet：returncode!=0 表示有改动（不早退）
            return _done(returncode=1)
        if "push" in args:
            return _done(returncode=1, stderr=push_stderr)
        # add / commit 等：成功
        return _done(returncode=0)

    return _fake_run


def test_auto_push_main_path_redacts_token_on_failure(monkeypatch):
    """main 分支 push 失败时，返回的 reason 不得含明文 GITHUB_TOKEN。"""
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setenv("GITHUB_TOKEN", _SECRET_TOKEN)
    monkeypatch.delenv("INVEST_PNL_PUSH_BRANCH", raising=False)  # 默认 main

    authed = f"https://x-access-token:{_SECRET_TOKEN}@github.com/owner/repo.git"
    push_stderr = f"fatal: unable to access '{authed}/': The requested URL returned error: 403"

    monkeypatch.setattr(subprocess, "run", _fake_git_run_factory(push_stderr))

    result = _auto_push_svg()

    assert result["pushed"] is False
    assert result["branch"] == "main"
    # 核心断言：token 明文绝不出现在返回值里（避免流到 scheduler 日志）
    assert _SECRET_TOKEN not in str(result)
    assert "x-access-token:***@" in result["reason"]


def test_auto_push_generic_except_redacts_token(monkeypatch):
    """兜底 `except Exception` 分支：非 CalledProcessError（OSError/TimeoutExpired
    等）的异常 message 里若带 authed_remote 的 token，返回的 reason 必须脱敏。

    驱动方式：让 authed_remote 构建完成（config --get 返回合法 https remote）后，
    main 分支的第一个 git 调用（git add）抛 OSError，且 message 里嵌入带 token 的 URL。
    """
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setenv("GITHUB_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("INVEST_PNL_PUSH_BRANCH", "main")

    authed = f"https://x-access-token:{_SECRET_TOKEN}@github.com/owner/repo.git"

    def _fake_run(cmd, **kwargs):
        args = cmd[1:] if cmd and cmd[0] == "git" else cmd
        if args[:2] == ["config", "--get"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/owner/repo.git\n", ""
            )
        # authed_remote 已构建完毕，下一步 git add 抛非 CPE 异常（带 token 的 URL）
        raise OSError(f"connection reset talking to {authed}")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = _auto_push_svg()

    assert result["pushed"] is False
    # 核心断言：明文 token 绝不出现在返回值里
    assert _SECRET_TOKEN not in str(result)
    assert "x-access-token:***@" in result["reason"]
    # type 名保留不脱敏
    assert "OSError" in result["reason"]


def test_auto_push_called_process_error_redacts_token(monkeypatch):
    """`except subprocess.CalledProcessError` 兜底分支：git 调用以 check=True 抛
    CalledProcessError 且 stderr 带 token 时，返回的 reason 必须脱敏。"""
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setenv("GITHUB_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("INVEST_PNL_PUSH_BRANCH", "main")

    authed = f"https://x-access-token:{_SECRET_TOKEN}@github.com/owner/repo.git"
    cpe_stderr = f"fatal: unable to access '{authed}/': The requested URL returned error: 403"

    def _fake_run(cmd, **kwargs):
        args = cmd[1:] if cmd and cmd[0] == "git" else cmd
        check = kwargs.get("check", False)
        if args[:2] == ["config", "--get"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/owner/repo.git\n", ""
            )
        # main 分支 git add 默认 check=True → 抛 CalledProcessError（带 token 的 stderr）
        if check:
            raise subprocess.CalledProcessError(1, cmd, "", cpe_stderr)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = _auto_push_svg()

    assert result["pushed"] is False
    # 核心断言：明文 token 绝不出现在返回值里
    assert _SECRET_TOKEN not in str(result)
    assert "x-access-token:***@" in result["reason"]
    assert result["reason"].startswith("git failure:")
