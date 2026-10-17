"""PnL snapshot 关键 helper 测试 — 时区判断 / 隐私脱敏 / 基准对齐。"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import shutil
import subprocess

import pytest

from openinvest.jobs import pnl_snapshot
from openinvest.jobs.pnl_snapshot import (
    Snapshot,
    _auto_push_svg,
    _is_trading_window,
    _outperform_events,
    _pct_label_pos,
    _redact_token_in,
)
from openinvest.calc.series import BenchmarkSeries


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


# ---------- outperform 事件真的产出非空结果（#197 回归：get_all_series 缺 start_date 被吞）----------

def test_outperform_events_nonempty_on_happy_path(monkeypatch):
    """正常路径下 _outperform_events 必须真的产出事件，而不是被吞异常静默退化成空列表。"""
    history = [
        {"ts": "2026-06-01T00:00:00Z", "equity_idx": 100.0},
        {"ts": "2026-07-01T00:00:00Z", "equity_idx": 105.0},
    ]
    series = BenchmarkSeries(
        key="纳指", color="#000", group="index", dash="",
        points={"2026-06-01": 0.0, "2026-07-01": 2.0},
    )

    captured_args = []

    def _fake_get_all_series(*args, **kwargs):
        captured_args.append(args)
        return [series]

    monkeypatch.setattr(pnl_snapshot, "_read_history", lambda window_days=None: history)
    monkeypatch.setattr(pnl_snapshot, "get_all_series", _fake_get_all_series)

    snap = Snapshot(ts="2026-07-01T00:00:00Z", equity_idx=105.0)
    events = _outperform_events(snap)

    assert captured_args == [("2026-06-01",)]  # start_date 必须传，不能裸调
    assert events != []
    assert events[0]["benchmark"] == "纳指"  # BenchmarkSeries 只有 .key，没有 .label
    # #234-3：两侧口径不同，对外 label 只写方向 + 口径说明，不量化差值
    assert events[0]["label"] == "作者账户过去 2 个数据点 跑赢纳指（口径不同，只看方向）"
    assert events[0]["user_pct"] == 5.0   # 窗口内总权益 100 → 105
    assert events[0]["diff_pct"] == 3.0  # 原始字段保留


# ---------- % 标签防与左侧名 label 重叠（openInvest#92 回归）----------
# 柱状图左轴 BAR_AXIS_LEFT=200，左侧 <200 是基准名 label 区。
# BAR_AXIS_RIGHT=720（W=800 - 80 右边距）。


def test_pct_label_full_width_negative_flips_inside():
    # 满宽负条 bar_x≈200，外侧标签会向左压住名 label → 翻到条内右生长
    x, anchor, fill = _pct_label_pos(
        -50.0, 200.0, 320.0, is_user=False, bar_axis_left=200, bar_axis_right=720,
    )
    assert anchor == "start"      # 条内、向右
    assert x >= 200               # 不进入左侧 label 区 (<200)
    assert fill == "#f0f6fc"      # 基准条上用浅色保证可读


def test_pct_label_full_width_negative_user_bar_uses_dark():
    _, _, fill = _pct_label_pos(
        -50.0, 200.0, 320.0, is_user=True, bar_axis_left=200, bar_axis_right=720,
    )
    assert fill == "#0d1117"      # 金色用户条改用深色


def test_pct_label_short_negative_stays_outside():
    # 短负条远离左轴，外侧放得下 → 维持外侧左生长、沿用条色 (None)
    assert _pct_label_pos(
        -3.0, 500.0, 18.0, is_user=False, bar_axis_left=200, bar_axis_right=720,
    ) == (494.0, "end", None)


def test_pct_label_positive_outside_unchanged():
    assert _pct_label_pos(
        12.0, 400.0, 120.0, is_user=False, bar_axis_left=200, bar_axis_right=720,
    ) == (526.0, "start", None)


# ---------- 正向满宽条 % 标签防右侧溢出 ----------

def test_pct_label_full_width_positive_flips_inside():
    """正向满宽条的 % 标签放到条端外侧会超出画布右边 → 翻到条内"""
    # bar 从 400 到 720 (bar_w=320)，外侧 x=726 + LABEL_W(56) = 782 <= 800 → OK
    x, anchor, fill = _pct_label_pos(
        50.0, 400.0, 320.0, is_user=False, bar_axis_left=200, bar_axis_right=720,
    )
    assert anchor == "start"
    assert fill is None

    # Bar 到 760 (bar_w=360), outside_x=766, 766+56=822 > 800 → flip
    x3, anchor3, fill3 = _pct_label_pos(
        99.0, 400.0, 360.0, is_user=False, bar_axis_left=200, bar_axis_right=720,
    )
    assert anchor3 == "end"       # 条内、向左
    assert x3 == 400.0 + 360.0 - 6  # 贴在条右端内侧
    assert fill3 == "#f0f6fc"     # 基准条用浅色


def test_pct_label_full_width_positive_user_bar_uses_dark():
    """正向满宽的用户条，翻入条内时用深色"""
    _, _, fill = _pct_label_pos(
        99.0, 400.0, 360.0, is_user=True, bar_axis_left=200, bar_axis_right=720,
    )
    assert fill == "#0d1117"


# ---------- _ensure_benchmarks_fresh（过期基准自动刷新）----------

def test_ensure_benchmarks_fresh_skips_when_cached_covers(monkeypatch):
    """缓存数据覆盖 start_date 时不调用 refresh"""
    from openinvest.jobs.pnl_snapshot import _ensure_benchmarks_fresh

    refresh_calls = []
    monkeypatch.setattr(
        "openinvest.jobs.pnl_snapshot.load_benchmark",
        lambda key: {"end": "2026-06-30"},
    )
    monkeypatch.setattr(
        "openinvest.jobs.pnl_snapshot.refresh_benchmark",
        lambda k, s, e: refresh_calls.append(k),
    )
    _ensure_benchmarks_fresh("2026-06-01", "2026-06-27")
    assert refresh_calls == []


def test_ensure_benchmarks_fresh_refreshes_stale(monkeypatch):
    """缓存 end < start_date 时自动刷新"""
    from openinvest.jobs.pnl_snapshot import _ensure_benchmarks_fresh

    refresh_calls = []
    monkeypatch.setattr(
        "openinvest.jobs.pnl_snapshot.load_benchmark",
        lambda key: {"end": "2026-04-27"},  # stale
    )
    monkeypatch.setattr(
        "openinvest.jobs.pnl_snapshot.refresh_benchmark",
        lambda k, s, e: refresh_calls.append(k),
    )
    _ensure_benchmarks_fresh("2026-05-28", "2026-06-27")
    from openinvest.core.benchmarks import BENCHMARKS
    assert len(refresh_calls) == len(BENCHMARKS)


def test_ensure_benchmarks_fresh_handles_no_cache(monkeypatch):
    """无缓存时自动刷新"""
    from openinvest.jobs.pnl_snapshot import _ensure_benchmarks_fresh

    refresh_calls = []
    monkeypatch.setattr("openinvest.jobs.pnl_snapshot.load_benchmark", lambda key: None)
    monkeypatch.setattr(
        "openinvest.jobs.pnl_snapshot.refresh_benchmark",
        lambda k, s, e: refresh_calls.append(k),
    )
    _ensure_benchmarks_fresh("2026-05-28", "2026-06-27")
    from openinvest.core.benchmarks import BENCHMARKS
    assert len(refresh_calls) == len(BENCHMARKS)


def test_ensure_benchmarks_fresh_swallows_errors(monkeypatch):
    """单个基准 refresh 失败不影响其他基准"""
    from openinvest.jobs.pnl_snapshot import _ensure_benchmarks_fresh

    refresh_calls = []
    fail_count = [0]
    monkeypatch.setattr("openinvest.jobs.pnl_snapshot.load_benchmark", lambda key: None)

    def _mock_refresh(k, s, e):
        if fail_count[0] == 0:
            fail_count[0] += 1
            raise ConnectionError("network down")
        refresh_calls.append(k)

    monkeypatch.setattr("openinvest.jobs.pnl_snapshot.refresh_benchmark", _mock_refresh)
    _ensure_benchmarks_fresh("2026-05-28", "2026-06-27")
    from openinvest.core.benchmarks import BENCHMARKS
    # 1 个失败，其余成功
    assert len(refresh_calls) == len(BENCHMARKS) - 1



def test_render_svg_no_holding_symbols(monkeypatch):
    """红线 #1 回归（issue #179 P1-C①）：公开 pnl-data 分支的 SVG 不得含
    可反推持仓的 symbol / 资产名（NDQ.AX、GC=F、Gold 等）。图例用泛化标签。"""
    import openinvest.jobs.pnl_snapshot as ps
    from openinvest.jobs.pnl_snapshot import render_svg

    # 与同文件 _ensure_benchmarks_fresh 系测试同款隔离：不碰真实基准缓存/网络
    monkeypatch.setattr(ps, "get_all_series", lambda start_date: [])
    monkeypatch.setattr(ps, "_ensure_benchmarks_fresh", lambda s, e: None)

    history = [
        {"ts": f"2026-06-{d:02d}T10:00:00+00:00", "equity_idx": 100.0 + d}
        for d in range(1, 6)
    ]
    svg = render_svg(history)
    for forbidden in ("NDQ", "GC=F", "GC_F", "Gold", "gold_cny", "资产 A", "资产 B"):
        assert forbidden not in svg, f"公开 SVG 含违禁词 {forbidden!r}（红线 #1）"
    assert "口径：★ 实盘" in svg, "口径脚注缺失（issue #179 P1-A③）"
    assert "<polyline" in svg and "总权益曲线" in svg


# ---------- orphan 分支 worktree：真 git、本地 bare 远端、零网络（#232-5 回归） ----------

def _g(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def pnl_clone(tmp_path, monkeypatch):
    """bare 远端带 pnl-data 分支 + **单分支 clone main**（refs/remotes/origin/pnl-data 不存在）。

    remote.origin.url 设成 GitHub https 形（_auto_push_svg 只认这种），再用 insteadOf
    把带 token 的 URL 改写到本地 bare；GIT_ALLOW_PROTOCOL=file 兜底——改写没生效就直接失败，
    绝不出网。
    """
    for k, v in {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_ALLOW_PROTOCOL": "file", "GIT_AUTHOR_NAME": "t",
                 "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"}.items():
        monkeypatch.setenv(k, v)
    bare, seed, clone = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "clone"
    _g("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _g("init", "-q", "-b", "main", str(seed), cwd=tmp_path)
    _g("commit", "-q", "--allow-empty", "-m", "main", cwd=seed)
    _g("push", "-q", str(bare), "main", cwd=seed)
    _g("checkout", "-q", "--orphan", "pnl-data", cwd=seed)
    (seed / "docs").mkdir()
    (seed / "docs" / "pnl_chart.svg").write_text("<svg>old</svg>")
    _g("add", "docs", cwd=seed)
    _g("commit", "-q", "-m", "old", cwd=seed)
    _g("push", "-q", str(bare), "pnl-data", cwd=seed)
    _g("clone", "-q", "--single-branch", "--branch", "main", str(bare), str(clone), cwd=tmp_path)
    _g("remote", "set-url", "origin", "https://github.com/owner/repo.git", cwd=clone)
    _g("config", f"url.{bare}.insteadOf",
       "https://x-access-token:tok@github.com/owner/repo.git", cwd=clone)
    assert _g("for-each-ref", "refs/remotes/origin/pnl-data", cwd=clone) == ""  # 前置：ref 缺失

    (clone / "docs").mkdir()
    (clone / "docs" / "pnl_chart.svg").write_text("<svg>new</svg>")
    monkeypatch.setattr(pnl_snapshot, "ROOT", clone)
    monkeypatch.setattr(pnl_snapshot, "SVG_PATH", clone / "docs" / "pnl_chart.svg")
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("INVEST_PNL_PUSH_BRANCH", "pnl-data")
    return bare, clone


def test_orphan_push_works_on_single_branch_clone(pnl_clone):
    """ls-remote 看得到 pnl-data 但本地没 tracking ref——旧代码 worktree add 静默失败，
    pushed 永远 False；现在先 fetch 建 ref，真的推上去。"""
    bare, clone = pnl_clone
    result = _auto_push_svg()
    assert result == {"pushed": True, "branch": "pnl-data", "mode": "orphan"}
    assert _g("show", "pnl-data:docs/pnl_chart.svg", cwd=bare) == "<svg>new</svg>"
    # 每次推一个无父 commit：公开仓库的 pnl-data 历史不随推送增长
    assert _g("rev-list", "--count", "pnl-data", cwd=bare) == "1"
    assert len(_g("worktree", "list", cwd=clone).splitlines()) == 1  # 临时 worktree 已清


def test_orphan_worktree_registration_self_heals(pnl_clone, tmp_path):
    """崩过一次留下的孤儿 worktree 登记（目录已被 TemporaryDirectory 删掉）仍占着
    pnl-data → 先 git worktree prune 自愈，而不是此后每次都 "already used by worktree"。"""
    bare, clone = pnl_clone
    _g("fetch", "-q", str(bare), "+refs/heads/pnl-data:refs/remotes/origin/pnl-data", cwd=clone)
    ghost = tmp_path / "ghost"
    _g("worktree", "add", "-q", str(ghost), "-b", "pnl-data", "origin/pnl-data", cwd=clone)
    shutil.rmtree(ghost)  # TemporaryDirectory 清掉了目录，但 worktree 登记还在

    result = _auto_push_svg()
    assert result["pushed"] is True, result
    assert _g("show", "pnl-data:docs/pnl_chart.svg", cwd=bare) != "<svg>old</svg>"


def test_run_status_surfaces_push_failure(monkeypatch, tmp_path):
    """job 顶层 status 反映 push 失败；自动推送关闭 / 无变化仍是 ok。"""
    from types import SimpleNamespace
    ps = pnl_snapshot
    monkeypatch.setattr(ps, "_is_trading_window", lambda: True)
    monkeypatch.setattr(ps, "MemoryStore", lambda: None)
    monkeypatch.setattr(ps, "_compute_snapshot", lambda store: SimpleNamespace(ts="t", equity_idx=101.0))
    monkeypatch.setattr(ps, "_append_history", lambda snap: None)
    monkeypatch.setattr(ps, "_read_history", lambda: [])
    monkeypatch.setattr(ps, "SVG_PATH", tmp_path / "docs" / "pnl_chart.svg")
    monkeypatch.setattr(ps, "render_svg", lambda history: "<svg/>")
    monkeypatch.setattr(ps, "_outperform_events", lambda snap: [])
    for push, status in [
        ({"pushed": False, "reason": "git failure: fatal: x"}, "push_failed"),
        ({"pushed": False, "reason": "GITHUB_TOKEN env 缺失"}, "push_failed"),
        ({"pushed": False, "reason": "no svg change", "branch": "pnl-data"}, "ok"),
        ({"pushed": False, "reason": "INVEST_PNL_AUTOPUSH != 1"}, "ok"),
        ({"pushed": True, "branch": "pnl-data", "mode": "orphan"}, "ok"),
    ]:
        monkeypatch.setattr(ps, "_auto_push_svg", lambda push=push: push)
        assert ps.run()["status"] == status, push


def test_called_process_error_with_bytes_stderr_is_reported(monkeypatch):
    """orphan 路径的裸 subprocess.run 不带 text=True → CalledProcessError.stderr 是 bytes。
    旧代码把 bytes 喂 _redact_token_in → TypeError 炸穿 job（生产 2026-08-12 17:00 实证），
    真正的 git 错误丢失。现在解码后照常脱敏上报。"""
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setenv("GITHUB_TOKEN", _SECRET_TOKEN)
    monkeypatch.setenv("INVEST_PNL_PUSH_BRANCH", "main")
    stderr = f"fatal: boom https://x-access-token:{_SECRET_TOKEN}@github.com/o/r.git".encode()

    def _fake_run(cmd, **kwargs):
        if cmd[1:3] == ["config", "--get"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r.git\n", "")
        raise subprocess.CalledProcessError(128, cmd, b"", stderr)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = _auto_push_svg()
    assert result["pushed"] is False
    assert "fatal: boom" in result["reason"]
    assert _SECRET_TOKEN not in result["reason"]


def test_persist_outperform_never_touches_readme(monkeypatch, tmp_path):
    """事件只落 jsonl。曾经同步改 README marker → 生产 checkout 变脏、invest-deploy 拒绝 pull。"""
    readme = tmp_path / "README.md"
    readme.write_text("<!-- OUTPERFORM_FEED_START -->\n<!-- OUTPERFORM_FEED_END -->\n", encoding="utf-8")
    monkeypatch.setattr(pnl_snapshot, "SVG_PATH", tmp_path / "docs" / "pnl_chart.svg")
    pnl_snapshot._persist_outperform([{"ts": "2026-10-10T10:00:00", "label": "x", "benchmark": "b"}])
    assert readme.read_text(encoding="utf-8") == "<!-- OUTPERFORM_FEED_START -->\n<!-- OUTPERFORM_FEED_END -->\n"
    assert len((tmp_path / "docs" / "outperform_events.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_unvalued_holding_skips_snapshot_with_its_own_reason(monkeypatch):
    """有持仓拉不到价 / 汇率 → 总权益少一块、曲线假跌：这次不记点，并说清原因（不泄露 symbol）"""
    from openinvest.jobs import pnl_snapshot as ps

    def boom(store):
        raise ps._Skip(reason="unvalued_holdings", count=1)
    monkeypatch.setattr(ps, "_compute_snapshot", boom)
    monkeypatch.setattr(ps, "_is_trading_window", lambda *a, **k: True)
    out = ps.run()
    assert out == {"status": "skipped", "reason": "unvalued_holdings", "count": 1}


# ---------- 总权益曲线：不再硬编码 NDQ.AX + GC=F（评审路线 Sprint 2 · C4） ----------
# 虚构持仓：A 股 ETF + 场外基金 + 美股（没有 NDQ / GC / AUD）。临时 memory + 假行情库 + mock 行情，不联网。

import json  # noqa: E402

import pandas as pd  # noqa: E402

from openinvest.core.memory_store import MemoryStore  # noqa: E402
from openinvest.services import account_performance as ap  # noqa: E402
from openinvest.utils.quotes import QuoteSnapshot  # noqa: E402

_DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
# 与 CI「脱敏聚合输出」步同一组（ci.yml）：公开产物里不得出现的字段名 / 子串
_CI_SENSITIVE = ['"symbol"', '"threshold"', '"verdict_id"', '"asset"', "NDQ", "GC=F"]
# 虚构持仓自己的标识 / 数量 / 成本：同样不得出现
_HOLDING_SECRETS = ["510300", "110020", "AAPL", "777.25", "1234.5", "31.375", "FUND:"]


class _FakeMarket:
    def __init__(self, table):
        self.table = table

    def get_history_df(self, symbol, days=730):
        if symbol not in self.table:
            return pd.DataFrame()
        closes = self.table[symbol]
        df = pd.DataFrame({"Close": list(closes.values())}, index=pd.to_datetime(list(closes.keys())))
        df.index.name = "Date"
        return df


def _fork_world(monkeypatch, tmp_path, *, extra_holdings=(), ledger=True):
    """虚构 fork 用户 + 假行情；返回可变价格表（测试里改它模拟盘中价格变化）。"""
    table = {
        "510300.SS": dict(zip(_DAYS, [4.0, 4.1, 4.2, 4.1, 4.3])),
        "FUND:110020": dict(zip(_DAYS, [1.5, 1.5, 1.52, 1.53, 1.55])),
        "AAPL": dict(zip(_DAYS, [200.0, 202.0, 201.0, 205.0, 210.0])),
        "USDCNY=X": dict(zip(_DAYS, [7.1] * 5)),
    }
    holdings = [
        {"symbol": "510300.SS", "kind": "etf", "units": 777.25, "avg_cost": 4.0, "cost_currency": "CNY"},
        {"symbol": "FUND:110020", "kind": "fund", "units": 1234.5, "avg_cost": 1.5, "cost_currency": "CNY",
         "proxy_kind": "eastmoney_fund"},
        {"symbol": "AAPL", "kind": "equity", "units": 31.375, "avg_cost": 200.0, "cost_currency": "USD"},
        *extra_holdings,
    ]
    store = MemoryStore()
    store.write("user", "user", {"display_name": "Fork", "risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 100.0}, "holdings": holdings}, "")
    if ledger:
        for sym, units, price, ccy in [("510300.SS", 777.25, 4.0, "CNY"), ("FUND:110020", 1234.5, 1.5, "CNY"),
                                       ("AAPL", 31.375, 200.0, "USD")]:
            store.append_history({"ts_origin": "2026-10-05T10:00:00", "action": "buy", "symbol": sym,
                                  "units": units, "price": price, "currency": ccy,
                                  "funding_source": "external_funding"})

    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeMarket(table))
    monkeypatch.setattr(ap, "_benchmark_symbol", lambda: "")
    # _refresh_market 的联网出口：get_quote / get_history_data / get_fx_rate
    monkeypatch.setattr("openinvest.utils.quotes.get_quote",
                        lambda h: QuoteSnapshot(symbol=h["symbol"], price=1.0, currency="CNY", unit="u"))
    monkeypatch.setattr(pnl_snapshot, "get_history_data", lambda s, p="5d", **kw: pd.DataFrame())
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate", lambda q, b="CNY", as_of_date=None: 7.1)
    # 渲染 / 事件的联网出口：基准缓存
    monkeypatch.setattr(pnl_snapshot, "_ensure_benchmarks_fresh", lambda s, e: None)
    monkeypatch.setattr(pnl_snapshot, "get_all_series", lambda start_date: [])
    monkeypatch.setattr(pnl_snapshot, "_is_trading_window", lambda *a, **k: True)
    monkeypatch.setattr(pnl_snapshot, "HISTORY_PATH", tmp_path / "state" / "pnl_history.jsonl")
    monkeypatch.setattr(pnl_snapshot, "SVG_PATH", tmp_path / "docs" / "pnl_chart.svg")
    return table


def test_run_draws_total_equity_for_any_holdings_and_leaks_nothing(monkeypatch, tmp_path):
    """没有 NDQ / GC 的持仓也能画出总权益曲线；公开 SVG / 事件 / 历史里没有 symbol、数量、成本。"""
    table = _fork_world(monkeypatch, tmp_path)

    results = []
    for aapl_close in (210.0, 216.0, 205.0):   # 盘中价变化 → 三个不同的点
        table["AAPL"]["2026-10-09"] = aapl_close
        results.append(pnl_snapshot.run())
    assert [r["status"] for r in results] == ["ok", "ok", "ok"]
    assert [r["history_points"] for r in results] == [1, 2, 3]

    svg = (tmp_path / "docs" / "pnl_chart.svg").read_text(encoding="utf-8")
    assert "<polyline" in svg and "总权益曲线" in svg and "100" in svg

    hist_lines = (tmp_path / "state" / "pnl_history.jsonl").read_text(encoding="utf-8").splitlines()
    points = [json.loads(line) for line in hist_lines]
    assert all(set(p) == {"ts", "equity_idx", "total_pnl_pct"} for p in points)
    idx = [p["equity_idx"] for p in points]
    assert len(set(idx)) == 3 and idx[1] > idx[0] > idx[2]   # 跟着 AAPL 收盘价走（先涨后跌）

    public = [svg, *hist_lines]
    events = tmp_path / "docs" / "outperform_events.jsonl"
    if events.exists():
        public.append(events.read_text(encoding="utf-8"))
    for text in public:
        for needle in _CI_SENSITIVE + _HOLDING_SECRETS:
            assert needle not in text, f"公开产物含敏感子串 {needle!r}（红线 #1）"
    # 同 CI：返回值（进 scheduler 日志）也不带百分比数字 / symbol
    for r in results:
        assert not any(n in str(r) for n in _HOLDING_SECRETS + ["NDQ", "GC=F"]), r


def test_run_skips_when_a_holding_cannot_be_valued(monkeypatch, tmp_path):
    """有持仓既没价也没成本价：总权益少一块、曲线会假跌——这次不记点（不泄露 symbol 到返回值）。"""
    _fork_world(monkeypatch, tmp_path, extra_holdings=[
        {"symbol": "ZZZ.XX", "kind": "equity", "units": 5.0, "avg_cost": 0.0, "cost_currency": "CNY"}])
    out = pnl_snapshot.run()
    assert out == {"status": "skipped", "reason": "unvalued_holdings", "count": 1}
    assert not (tmp_path / "state" / "pnl_history.jsonl").exists()


def test_run_skips_without_ledger_history(monkeypatch, tmp_path):
    """没有账本 / 不足 2 个估值日：说清原因，不画空曲线也不崩。"""
    _fork_world(monkeypatch, tmp_path, ledger=False)
    out = pnl_snapshot.run()
    assert out["status"] == "skipped" and out["reason"] == "insufficient_history"


def test_read_history_ignores_legacy_two_asset_points(monkeypatch, tmp_path):
    """旧版点（total_pnl_pct = 两个固定资产的成本收益率，无 equity_idx）口径不同：不混画、不当窗口起点。"""
    path = tmp_path / "pnl_history.jsonl"
    now = datetime.now().astimezone()
    rows = [
        {"ts": (now - timedelta(days=3)).isoformat(), "total_pnl_pct": 12.0, "ndq_pnl_pct": 9.0, "gold_pnl_pct": 3.0},
        {"ts": (now - timedelta(days=2)).isoformat(), "equity_idx": 101.0, "total_pnl_pct": 1.0},
        {"ts": (now - timedelta(days=1)).isoformat(), "equity_idx": 102.0, "total_pnl_pct": 2.0},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(pnl_snapshot, "HISTORY_PATH", path)
    assert [e["equity_idx"] for e in pnl_snapshot._read_history()] == [101.0, 102.0]


def test_equity_curve_is_rebased_to_window_start():
    from openinvest.jobs.pnl_render import _equity_rel, _window_pct
    hist = [{"ts": "2026-10-01T10:00:00", "equity_idx": 102.0},
            {"ts": "2026-10-02T10:00:00", "equity_idx": 104.04}]
    rel = _equity_rel(hist)
    assert rel[0]["rel_pct"] == 0.0 and rel[1]["rel_pct"] == pytest.approx(2.0)
    assert _window_pct(hist) == pytest.approx(2.0)
    assert _window_pct(hist[:1]) is None and _equity_rel([]) == []
    # 不足 2 个点 → 占位图，不画孤零零一个点
    assert "数据采集中" in pnl_snapshot._render_svg_pure(hist[:1], [])


def test_first_run_after_deploy_keeps_published_svg_until_two_points(monkeypatch, tmp_path):
    """部署后第一轮只有 1 个新点（旧点口径不同被过滤）：已发布的旧图不被占位图覆盖，第二轮才换成新曲线。"""
    table = _fork_world(monkeypatch, tmp_path)
    svg = tmp_path / "docs" / "pnl_chart.svg"
    svg.parent.mkdir(parents=True)
    svg.write_text("<svg>previously published</svg>", encoding="utf-8")

    assert pnl_snapshot.run()["history_points"] == 1
    assert svg.read_text(encoding="utf-8") == "<svg>previously published</svg>"

    table["AAPL"]["2026-10-09"] = 216.0
    assert pnl_snapshot.run()["history_points"] == 2
    assert "<polyline" in svg.read_text(encoding="utf-8")


def test_run_does_not_swallow_performance_errors(monkeypatch, tmp_path):
    """build_account_performance 抛异常要让 job 报 failed（scheduler 写 job_runs、watchdog 能看到），
    而不是悄悄 skipped——静默死过一次（event_watch 卡死 21 天）。"""
    _fork_world(monkeypatch, tmp_path)

    def boom(pm, **kw):
        raise RuntimeError("db gone")
    monkeypatch.setattr(pnl_snapshot, "build_account_performance", boom)
    with pytest.raises(RuntimeError, match="db gone"):
        pnl_snapshot.run()
