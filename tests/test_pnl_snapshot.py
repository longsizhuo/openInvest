"""PnL snapshot 关键 helper 测试 — 时区判断 / 隐私脱敏 / 基准对齐。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from jobs.pnl_snapshot import (
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
