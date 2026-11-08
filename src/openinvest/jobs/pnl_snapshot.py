"""PnL 快照 + SVG 折线图生成

工作日交易时段每 2 小时跑一次。流程：
1. 刷新持仓行情 / 汇率进行情库（account_performance 只读库）
2. 复用 services.account_performance（账本重放 + 行情库估值，status / 日报同一份）取账户
   时间加权收益链接指数 → 总权益曲线上的一个点（基准币种，成立日 = 100；剔除存取款，含现金）
3. append 到 memory/.state/pnl_history.jsonl（git ignore）
4. 渲染 docs/pnl_chart.svg（入 git，窗口首点归一化为 100，只含一条账户整体曲线 + 基准柱）
5. （可选）自动 git commit + push 让 GitHub README 实时更新

账户整体，不认任何 symbol：任何持仓组合（A 股 ETF / 场外基金 / 美股 / 黄金 / 加密 ...）都画同一条
总权益曲线。以前硬编码只画 NDQ.AX + GC=F 两个标的，别人的持仓画出来是空的。

隐私设计：
- 原始 jsonl 只存指数（无 symbol / 数量 / 金额）→ gitignore，永不入库
- SVG 只含一条相对曲线 + 基准柱，axis 上不写任何数字 / 日期 / symbol / 持仓数量 / 阈值
- 看图能看出整体涨跌趋势，但读不出"持有什么 / 各持多少 / 资产规模多大"（CI 脱敏 grep 同款子串守着）

自动 push（可选）：
- 设 INVEST_PNL_AUTOPUSH=1 + GITHUB_TOKEN=ghp_xxx 启用
- INVEST_PNL_PUSH_BRANCH=main（默认）：commit 到主分支，git log 会有每小时一条
  "chore(pnl): hourly snapshot" 噪音；但 README 引用相对路径 `docs/pnl_chart.svg`
  GitHub 自动渲染最新版。
- INVEST_PNL_PUSH_BRANCH=pnl-data：用单独 orphan 分支只放 SVG，主分支干净；
  README 改用 raw URL 引用：
    https://raw.githubusercontent.com/<owner>/<repo>/pnl-data/docs/pnl_chart.svg

触发方式：
- jobs/pnl_snapshot.yml 自动每小时跑
- 或手动: python -m jobs.pnl_snapshot
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

from openinvest.core.benchmarks import (
    BENCHMARKS,
    BenchmarkSeries,
    get_all_series,
    load_benchmark,
    refresh_benchmark,
)
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import MemoryNotInitialized, PortfolioManager
from openinvest.services.account_performance import build_account_performance
from openinvest.services.financial_publication import historical_performance_publication_block as _publication_source_block
from openinvest.services.financial_publication import (
    SOURCE_STATEMENT,
    derived_publication_block,
    qualified_history,
    qualified_point,
    freeze_render_artifact,
    frozen_artifact_block,
    VERIFICATION_PENDING_SVG,
    seal_calculated_point,
    seal_outperform_event,
    seal_render_manifest,
    validate_selected_points,
)
from openinvest.utils.base_currency import base_currency
from openinvest.utils.eastmoney_fund import canonical_fund_symbol
from openinvest.utils.exchange_fee import get_history_data
# 渲染/事件计算纯核已迁 jobs/pnl_render（ADR-026）——导回保持历史导出面；
# 本文件只剩 IO shell + 三个同名薄包装（render_svg/_is_trading_window/
# _outperform_events，在本命名空间解析 IO 依赖，测试的 patch 才打得到）。
from openinvest.jobs.pnl_render import (  # noqa: F401
    BAR_BOTTOM_PAD,
    BAR_ROW_H,
    BAR_TOP_PAD,
    LINE_H,
    MARGIN_B,
    MARGIN_L,
    MARGIN_R,
    MARGIN_T,
    PLOT_H,
    PLOT_W,
    W,
    WINDOW_DAYS,
    Snapshot,
    _equity_rel,
    _latest_pct,
    _pct_label_pos,
    _project_y,
    _series_polyline,
    _window_pct,
)
from openinvest.jobs.pnl_render import _is_trading_window as _is_trading_window_pure
from openinvest.jobs.pnl_render import _outperform_events as _outperform_events_pure
from openinvest.jobs.pnl_render import render_svg as _render_svg_pure
from openinvest.paths import INVEST_ROOT
from openinvest.utils import oslock

ROOT = INVEST_ROOT
HISTORY_PATH = ROOT / "memory" / ".state" / "pnl_history.jsonl"
SVG_PATH = ROOT / "docs" / "pnl_chart.svg"

class _Skip(Exception):
    """这次不记快照（不编数）：run() 把 info 原样当 skipped 结果返回。"""

    def __init__(self, **info: Any) -> None:
        super().__init__(info)
        self.info = info


def _refresh_market(pm: PortfolioManager, base: str) -> None:
    """把持仓行情 / 汇率追到最新。

    account_performance 只读行情库、不联网；原先 _safe_close 顺手刷库的事要显式做。
    单个标的拉失败只告警：那天缺价由 account_performance 前值填充并计入 data_quality。
    """
    from openinvest.utils.fx import get_fx_rate
    from openinvest.utils.quotes import get_quote, is_eastmoney_fund
    held = [h for h in pm.holdings if h.get("symbol") and not h.get("is_tracking_only")]
    for h in held:
        try:
            get_quote(h)
            if is_eastmoney_fund(h):   # get_quote 对场外基金不写库，净值历史单独刷
                get_history_data(canonical_fund_symbol(str(h["symbol"])), "5d")
        except Exception as e:  # noqa: BLE001
            log.warning("pnl_snapshot 刷新行情失败: %s", e)
    ccys = {str(c).upper() for c in pm.cash} | {str(h.get("cost_currency") or "").upper() for h in held}
    for ccy in sorted(ccys - {base, ""}):
        get_fx_rate(ccy, base)


def _compute_snapshot(store: MemoryStore) -> Optional[Snapshot]:
    """总权益曲线上的一个点 = 账户时间加权收益链接指数（成立日 = 100）。

    复用 services.account_performance，不再自己按资产拼一份（以前硬编码 NDQ.AX + GC=F）。
    用 TWR 而不是裸 NAV：裸 NAV 会把入金画成涨幅、出金画成亏损。
    拉不到价 / 汇率的持仓会让总权益少一块、曲线假跌——这种点宁可不记（_Skip）。
    """
    try:
        pm = PortfolioManager(store)
    except MemoryNotInitialized:
        return None
    base = base_currency()
    if blocked := _publication_source_block(pm=pm):
        raise _Skip(**blocked)
    from openinvest.services.ledger_evidence import account_source_status
    ledger_before = account_source_status(pm)
    _refresh_market(pm, base)
    perf = build_account_performance(pm, base=base)
    if perf.get("status") != "ok" or perf.get("twr_all") is None:
        raise _Skip(reason="insufficient_history", detail=perf.get("reason"))
    unvalued_n = len((perf.get("data_quality") or {}).get("unvalued") or [])
    if unvalued_n:
        raise _Skip(reason="unvalued_holdings", count=unvalued_n)
    snap = Snapshot(
        ts=datetime.now().astimezone().isoformat(timespec="microseconds"),
        equity_idx=round(100 * (1 + float(perf["twr_all"]) / 100), 4),
    )
    try:
        snap.calculation_receipt = seal_calculated_point(store,
            {"ts": snap.ts, "equity_idx": snap.equity_idx}, perf, ledger_before)
    except (ValueError, TypeError):
        raise _Skip(**derived_publication_block("unverified_calculation_inputs"))
    return snap


def _append_history(snap: Snapshot) -> None:
    row = qualified_point(MemoryStore(), {
        "ts": snap.ts, "equity_idx": snap.equity_idx,
        "total_pnl_pct": round(snap.equity_idx - 100, 4), "calculation_receipt": snap.calculation_receipt,
    })
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_history(window_days: int = WINDOW_DAYS) -> List[Dict[str, Any]]:
    return qualified_history(MemoryStore(), HISTORY_PATH, window_days=window_days)


def _ensure_benchmarks_fresh(start_date: str, end_date: str) -> None:
    """Auto-refresh benchmark caches that don't cover [start_date, end_date].

    Without this, if cached benchmark data ends before the PnL history window
    starts, ``to_pct_series`` returns empty → all benchmark bars disappear.
    Only refreshes stale entries; already-fresh caches are left untouched.
    Network failures are swallowed per-key (same as refresh_benchmark design).
    """
    for key in BENCHMARKS:
        cached = load_benchmark(key)
        # 新鲜判定必须对齐窗口**终点**：旧条件 `end >= start_date` 在缓存刚进
        # 窗口起点后就永远为真，缓存最多冻结 WINDOW_DAYS=30 天而用户线每 2h 更新
        # （2026-07-18 实锤：9 个缓存全停 2026-06-26 达 22 天，公开图拿今日 PnL
        # 对比 6-26 基准）。#92 修的是"缓存早于窗口起点 → bar 消失"，这里补
        # "缓存落后窗口终点 → bar 冻结"。留 1 天余量容忍非交易日。
        if cached and cached.get("end", "") >= end_date:
            continue  # cache reaches the window end — genuinely fresh
        try:
            refresh_benchmark(key, start_date, end_date)
        except Exception as exc:
            log.warning("benchmark refresh failed for %s: %s", key, exc)


def render_svg(history: List[Dict[str, Any]]) -> str:
    """IO 包装：刷基准缓存 + 取基准序列，再委托 pnl_render 纯渲染。

    _ensure_benchmarks_fresh / get_all_series 在本命名空间解析——
    tests/test_pnl_snapshot 的 monkeypatch 依赖这一点。
    """
    history = validate_selected_points(MemoryStore(), history)
    if history:
        start_date = history[0]["ts"][:10]
        end_date = history[-1]["ts"][:10]
        # 自动刷新过期基准缓存 — 缓存数据不覆盖当前渲染窗口时重拉
        # （openInvest#92 根因之一：缓存 Apr 27 结束，窗口从 May 28 开始 → 0 条基准）
        _ensure_benchmarks_fresh(start_date, end_date)
        benchmark_series = get_all_series(start_date)
    else:
        benchmark_series = []
    svg = _render_svg_pure(history, benchmark_series)
    svg = svg.replace("★ 我的实盘", "★ 客户账本计算").replace("口径：★ 实盘", "口径：★ 客户账本计算")
    dimensions = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if dimensions is None:
        raise ValueError("financial SVG canvas unavailable")
    height = float(dimensions.group(2))
    svg = svg.replace(dimensions.group(0), f'viewBox="0 0 {dimensions.group(1)} {height + 24:g}"', 1)
    svg = svg.replace(f'height="{dimensions.group(2)}"', f'height="{height + 24:g}"', 1)
    return svg.replace("</svg>", f'<text x="{MARGIN_L}" y="{height + 12:g}" fill="#6e7681" class="label">{SOURCE_STATEMENT}</text>\n</svg>')


def _is_trading_window(now: Optional[datetime] = None) -> bool:
    """IO 包装：now 未传取当前时钟，委托 pnl_render 纯核（口径 docstring 见彼处）。"""
    from datetime import timezone
    return _is_trading_window_pure(now if now is not None else datetime.now(timezone.utc))


def _outperform_events(snap: Snapshot) -> List[Dict[str, Any]]:
    """IO 包装：读 history + 拉基准序列，再委托 pnl_render 纯核。"""
    history = _read_history(window_days=WINDOW_DAYS)
    if not history:
        return []
    start_date = history[0]["ts"][:10]
    try:
        all_series = get_all_series(start_date)
    except Exception as e:
        log.warning(f"benchmark series 拉失败，跳过 outperform 事件: {e}")
        return []
    return _outperform_events_pure(snap, history, all_series)



def _redact_token_in(text: str) -> str:
    """脱敏 git stderr 里可能出现的 'https://x-access-token:gho_xxx@github.com/...'
    避免 GITHUB_TOKEN 流到 scheduler 日志（audit security M1）"""
    import re as _re
    return _re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)


def _with_pending_notice(block: Dict[str, Any]) -> Dict[str, Any]:
    """Opted-in publishers replace an unqualified public curve with fixed status text."""
    if os.getenv("INVEST_PNL_AUTOPUSH", "0") != "1":
        return block
    return {**block, "withdrawal": _publish_svg_buffer(VERIFICATION_PENDING_SVG, notice_only=True)}


def _notice_if_existing_chart_unverified(info: Dict[str, Any]) -> Dict[str, Any]:
    """A skipped calculation must not leave a revoked public curve current."""
    if os.getenv("INVEST_PNL_AUTOPUSH", "0") != "1":
        return info
    if blocked := _publication_source_block():
        return _with_pending_notice(blocked)
    try:
        artifact = freeze_render_artifact(MemoryStore(), SVG_PATH, HISTORY_PATH)
        blocked = frozen_artifact_block(MemoryStore(), artifact)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        blocked = derived_publication_block("unverified_render_manifest")
    return _with_pending_notice(blocked) if blocked else info


def _auto_push_svg() -> Dict[str, Any]:
    if os.getenv("INVEST_PNL_AUTOPUSH", "0") != "1":
        return {"pushed": False, "reason": "INVEST_PNL_AUTOPUSH != 1"}
    if blocked := _publication_source_block():
        return _with_pending_notice(blocked)
    try:
        artifact = freeze_render_artifact(MemoryStore(), SVG_PATH, HISTORY_PATH)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return _with_pending_notice(derived_publication_block("unverified_render_manifest"))
    return _publish_svg_buffer(artifact.content, artifact=artifact)


def _publish_svg_buffer(svg_bytes: bytes, *, artifact=None, notice_only: bool = False) -> Dict[str, Any]:
    """Stage only frozen bytes in a private index; never read the SVG path again."""
    def result(value):
        return {**value, **({"publication_kind": "verification_notice", "historical_performance_eligible": False,
                            "learning_eligible": False, "prediction_eligible": False} if notice_only else {})}

    if os.getenv("INVEST_PNL_AUTOPUSH", "0") != "1":
        return result({"pushed": False, "reason": "INVEST_PNL_AUTOPUSH != 1"})
    if notice_only:
        if svg_bytes != VERIFICATION_PENDING_SVG:
            return result(derived_publication_block("invalid_verification_notice_payload"))
    else:
        if artifact is None or artifact.content != svg_bytes:
            return derived_publication_block("unverified_frozen_artifact")
        if blocked := frozen_artifact_block(MemoryStore(), artifact):
            return _with_pending_notice(blocked)
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return result({"pushed": False, "reason": "GITHUB_TOKEN env 缺失"})
    branch = os.getenv("INVEST_PNL_PUSH_BRANCH", "main").strip() or "main"
    orphan = branch != "main"

    def _git(args, *, check=True, index=None, binary=False, data=None):
        return subprocess.run(
            ["git", *args], cwd=str(ROOT), capture_output=True, text=not binary,
            check=check, input=data,
            env={**os.environ, "GIT_INDEX_FILE": str(index)} if index is not None else None,
        )

    class _BlobMismatch(ValueError):
        pass

    def _stage(index, name, content):
        oid = _git(["hash-object", "-w", "--stdin"], binary=True, data=content).stdout.decode("ascii").strip()
        _git(["update-index", "--add", "--cacheinfo", "100644", oid, name], index=index)

    try:
        remote = _git(["config", "--get", "remote.origin.url"]).stdout.strip()
        if not remote.startswith("https://github.com/"):
            return result({"pushed": False, "reason": f"only https github remote supported, got {remote}"})
        authed_remote = remote.replace("https://", f"https://x-access-token:{token}@", 1)
        exists = bool(_git(["ls-remote", "--heads", authed_remote, branch], check=False).stdout.strip())
        if orphan:
            _git(["worktree", "prune"], check=False)
        if exists:
            ref = f"refs/remotes/origin/{branch}"
            _git(["fetch", authed_remote, f"+refs/heads/{branch}:{ref}"])
            base = _git(["rev-parse", ref]).stdout.strip()
        else:
            base = None if orphan else _git(["rev-parse", "HEAD"]).stdout.strip()

        import tempfile
        with tempfile.TemporaryDirectory(prefix="openinvest-pnl-index-") as directory:
            index = Path(directory) / "index"
            _git(["read-tree", base] if base else ["read-tree", "--empty"], index=index)
            _stage(index, "docs/pnl_chart.svg", svg_bytes)
            if orphan:
                _stage(index, "README.md", (
                    "# pnl-data branch\n\nThis orphan branch holds the auto-generated PnL chart only. "
                    "Do not commit code here. Updated hourly by `jobs/pnl_snapshot`.\n"
                ).encode("utf-8"))
            # Verify the actual staged blob and then the immutable tree that is
            # passed to commit-tree. Neither operation rereads the SVG path.
            if _git(["show", ":docs/pnl_chart.svg"], index=index, binary=True).stdout != svg_bytes:
                raise _BlobMismatch()
            if base and _git(["diff", "--cached", "--quiet", base], check=False, index=index).returncode == 0:
                return result({"pushed": False, "reason": "no svg change", "branch": branch})
            tree = _git(["write-tree"], index=index).stdout.strip()
            if _git(["show", f"{tree}:docs/pnl_chart.svg"], binary=True).stdout != svg_bytes:
                raise _BlobMismatch()
            if not notice_only and (blocked := frozen_artifact_block(MemoryStore(), artifact)):
                return _with_pending_notice(blocked)
            commit = _git([
                "-c", "user.name=pnl-bot", "-c", "user.email=pnl-bot@invest.local",
                "commit-tree", tree, *([] if orphan else ["-p", base]),
                "-m", "chore(pnl): hourly snapshot [skip ci]",
            ]).stdout.strip()
            if not notice_only and (blocked := frozen_artifact_block(MemoryStore(), artifact)):
                return _with_pending_notice(blocked)
            push = _git(["push", *(["--force"] if orphan else []), authed_remote,
                         f"{commit}:refs/heads/{branch}"], check=False)
            if push.returncode != 0:
                return result({"pushed": False, "reason": f"push failed: {_redact_token_in(push.stderr[:200])}",
                               "branch": branch})
            return result({"pushed": True, "branch": branch, "mode": "orphan" if orphan else "main"})
    except _BlobMismatch:
        return result(derived_publication_block("staged_svg_mismatch"))
    except subprocess.CalledProcessError as exc:
        raw = exc.stderr[:200] if exc.stderr else str(exc)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return result({"pushed": False, "reason": f"git failure: {_redact_token_in(raw)}"})
    except Exception as exc:
        return result({"pushed": False,
                       "reason": f"unexpected: {type(exc).__name__}: {_redact_token_in(str(exc))}"})


# 不 push 的正常原因；其余 pushed=False 都是失败。#232-5：之前失败只埋在 push 子 dict 里，
# job 顶层照报 ok，没人会去翻——顶层 status 必须反映出来。
_PUSH_NOOP_REASONS = ("INVEST_PNL_AUTOPUSH != 1", "no svg change")


def _push_status(push: Dict[str, Any]) -> str:
    if push.get("status") in {"unverified", "simulation_only"}:
        return push["status"]
    if push.get("pushed") or push.get("reason") in _PUSH_NOOP_REASONS:
        return "ok"
    log.warning(f"[pnl_snapshot] SVG push 失败: {push.get('reason')}")
    return "push_failed"


def _persist_outperform(events: List[Dict[str, Any]]) -> None:
    """事件落盘 → docs/outperform_events.jsonl（append-only），供 /api/outperform_events 读。

    ponytail: 曾经同步刷 README 的 OUTPERFORM_FEED marker，但两种 push 模式都不提交 README，
    feed 从没上过 GitHub，只会弄脏生产 checkout、卡住 invest-deploy（2026-10-10 删除）。
    """
    if not events:
        return
    history = _read_history()
    sealed_events = [seal_outperform_event(MemoryStore(), event, HISTORY_PATH, history) for event in events]
    out_path = SVG_PATH.parent / "outperform_events.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8", newline="\n") as f:
        for ev in sealed_events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def run() -> Dict[str, Any]:
    """job entry：算快照 + 写历史 + 渲染 SVG + 可选自动 push"""
    # 跳过非交易时段（周末 / 凌晨）
    if not _is_trading_window():
        return _notice_if_existing_chart_unverified({"status": "skipped", "reason": "non_trading_window",
                "now": datetime.now().isoformat(timespec="seconds")})

    store = MemoryStore()
    try:
        snap = _compute_snapshot(store)
    except _Skip as e:
        info = {"status": "skipped", **e.info}
        return (_with_pending_notice(info) if info["status"] in {"unverified", "simulation_only"}
                else _notice_if_existing_chart_unverified(info))
    if snap is None:
        return _notice_if_existing_chart_unverified({"status": "skipped", "reason": "no_portfolio"})

    try:
        _append_history(snap)
        history = _read_history()
    except (OSError, ValueError, KeyError, TypeError):
        return _with_pending_notice(derived_publication_block("unverified_point_receipt"))
    if len(_equity_rel(history)) < 2:
        return _with_pending_notice(derived_publication_block("insufficient_qualified_history", points=len(history)))
    window_pct = _window_pct(history)

    # 渲染并原子写入 SVG。部署后第一轮只有 1 个新点（旧点口径不同被过滤）：已有旧图就先留着，
    # 别用「数据采集中」占位图覆盖已发布的图（autopush 会推上 README）——2h 后下一轮就有 2 个点。
    if len(_equity_rel(history)) >= 2 or not SVG_PATH.exists():
        SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            svg_content = render_svg(history)
            tmp = SVG_PATH.with_suffix(".svg.tmp")
            tmp.write_text(svg_content, encoding="utf-8", newline="\n")
            oslock.replace(tmp, SVG_PATH)
            seal_render_manifest(store, SVG_PATH, HISTORY_PATH, history, window_days=WINDOW_DAYS)
        except (OSError, ValueError, KeyError, TypeError):
            return _with_pending_notice(derived_publication_block("unverified_render_manifest"))

    # 跑赢基准的"可分享瞬间"事件（PM-3 增长杠杆）
    try:
        events = _outperform_events(snap)
        _persist_outperform(events)
    except (OSError, ValueError, KeyError, TypeError):
        return _with_pending_notice(derived_publication_block("unverified_event_sources"))

    # 可选：commit + push 到 GitHub（受 INVEST_PNL_AUTOPUSH env 控制）
    push_result = _auto_push_svg()

    return {
        "status": _push_status(push_result),
        "ts": snap.ts,
        "history_points": len(history),
        "svg_path": str(SVG_PATH),
        # **故意不在 return 里暴露百分比数字**，避免 scheduler 日志泄露
        "trend": "up" if (window_pct or 0) > 0 else ("down" if (window_pct or 0) < 0 else "flat"),
        "outperform_count": len(events),
        # 不直接返回 events 内容（避免 logger 暴露具体涨幅），交由
        # /api/outperform_events 端点按需读取
        "push": push_result,
    }


def render_only() -> Dict[str, Any]:
    """只读现有 history → 重渲染 SVG → 可选 push，不 append 新 entry。

    场景：清理过 pnl_history.jsonl 噪声后，想重新生成图但不想再追加新点
    （尤其当前是非交易时段）。
    """
    if blocked := _publication_source_block():
        return _with_pending_notice(blocked)
    try:
        history = _read_history()
    except (OSError, ValueError, KeyError, TypeError):
        return _with_pending_notice(derived_publication_block("unverified_point_receipt"))
    if len(_equity_rel(history)) < 2:
        return _with_pending_notice(derived_publication_block("insufficient_qualified_history", points=len(history)))
    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        svg_content = render_svg(history)
        tmp = SVG_PATH.with_suffix(".svg.tmp")
        tmp.write_text(svg_content, encoding="utf-8", newline="\n")
        oslock.replace(tmp, SVG_PATH)
        seal_render_manifest(MemoryStore(), SVG_PATH, HISTORY_PATH, history, window_days=WINDOW_DAYS)
    except (OSError, ValueError, KeyError, TypeError):
        return _with_pending_notice(derived_publication_block("unverified_render_manifest"))
    push_result = _auto_push_svg()
    push_status = _push_status(push_result)
    return {
        "status": "ok_render_only" if push_status == "ok" else push_status,
        "history_points": len(history),
        "svg_path": str(SVG_PATH),
        "push": push_result,
    }


if __name__ == "__main__":
    import sys
    if "--render-only" in sys.argv:
        print(render_only())
    else:
        print(run())
