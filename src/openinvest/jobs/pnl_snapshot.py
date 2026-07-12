"""PnL 快照 + SVG 折线图生成

每小时跑一次。流程：
1. 读 portfolio.md（cash + ndq + gold）
2. 拉当前价（NDQ.AX / AUDCNY / 黄金现货 GC=F + USDCNY）
3. 算各类资产的浮盈百分比（**只算 %，不存绝对金额到 git**）
4. append 到 memory/.state/pnl_history.jsonl（git ignore）
5. 渲染 docs/pnl_chart.svg（入 git，但只含百分比线段，无明文数字）
6. （可选）自动 git commit + push 让 GitHub README 实时更新

隐私设计：
- 原始 jsonl 含 cash 等绝对值 → gitignore，永不入库
- SVG 只含百分比线段，且 axis 上不写任何数字 / 日期
- 看图能看出涨跌趋势 + 哪个资产贡献多，但读不出"今天浮盈多少元 / 资产规模多大"

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
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from dotenv import load_dotenv

from openinvest.core.benchmarks import (
    BENCHMARKS,
    BenchmarkSeries,
    get_all_series,
    load_benchmark,
    refresh_benchmark,
)
from openinvest.core.memory_store import MemoryStore
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
    _latest_pct,
    _pct_label_pos,
    _project_y,
    _series_polyline,
)
from openinvest.jobs.pnl_render import _is_trading_window as _is_trading_window_pure
from openinvest.jobs.pnl_render import _outperform_events as _outperform_events_pure
from openinvest.jobs.pnl_render import render_svg as _render_svg_pure
from openinvest.utils.gold_price import get_gold_snapshot
from openinvest.paths import INVEST_ROOT

load_dotenv()

ROOT = INVEST_ROOT
HISTORY_PATH = ROOT / "memory" / ".state" / "pnl_history.jsonl"
SVG_PATH = ROOT / "docs" / "pnl_chart.svg"

def _get_gold_offset_from_strategy(store: MemoryStore) -> float:
    """从 strategy.md 的 target_assets[gold] 拿 price_offset_pct。

    让 gold_now 与用户买入价同口径（浙商点差）。找不到时退回 0.0（spot 价）。
    """
    strategy = store.read("strategy")
    if strategy is None:
        return 0.0
    for asset in strategy.get("target_assets", []) or []:
        if asset.get("symbol") == "GC=F":
            return float(asset.get("price_offset_pct", 0.0) or 0.0)
    return 0.0


def _safe_close(symbol: str) -> Optional[float]:
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def _compute_snapshot(store: MemoryStore) -> Optional[Snapshot]:
    """v2 通用化：读 holdings 数组而不是写死字段"""
    portfolio = store.read("portfolio")
    if portfolio is None:
        return None

    # 优先读 v2 cash dict + holdings list；不存在时 fallback 到 v1 扁平字段
    cash_dict = dict(portfolio.get("cash") or {})
    holdings_list = list(portfolio.get("holdings") or [])

    if cash_dict:
        cash_cny = float(cash_dict.get("CNY", 0) or 0)
        aud_cash = float(cash_dict.get("AUD", 0) or 0)
    else:
        cash_cny = float(portfolio.get("cash_cny", 0) or 0)
        aud_cash = float(portfolio.get("aud_cash", 0) or 0)

    if holdings_list:
        ndq_h = next((h for h in holdings_list if h.get("symbol") == "NDQ.AX"), None)
        gold_h = next((h for h in holdings_list if h.get("symbol") == "GC=F"), None)
        ndq_shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
        ndq_avg = float(ndq_h.get("avg_cost", 0) or 0) if ndq_h else 0.0
        gold_grams = float(gold_h.get("units", 0) or 0) if gold_h else 0.0
        gold_avg = float(gold_h.get("avg_cost", 0) or 0) if gold_h else 0.0
    else:
        ndq_shares = float(portfolio.get("ndq_shares", 0) or 0)
        ndq_avg = float(portfolio.get("ndq_avg_cost_aud_per_share", 0) or 0)
        gold_grams = float(portfolio.get("gold_grams", 0) or 0)
        gold_avg = float(portfolio.get("gold_avg_cost_cny_per_gram", 0) or 0)

    audcny = _safe_close("AUDCNY=X") or 4.7
    ndq_price = _safe_close("NDQ.AX")
    # 用 strategy.target_assets[gold].price_offset_pct 让 gold_now（"现在按浙商克价
    # 算的估值价"）与用户实际买入价同口径，避免 spot vs bank 不一致导致系统性
    # 偏低 1-1.5% 浮盈（audit financial C1）
    gold_offset = _get_gold_offset_from_strategy(store)
    snap = get_gold_snapshot(offset_pct=gold_offset)
    gold_now = snap.bank_cny_per_gram if snap else None

    # 各资产浮盈 %
    ndq_pnl_pct = (
        ((ndq_price / ndq_avg) - 1) * 100
        if (ndq_price and ndq_avg > 0 and ndq_shares > 0) else None
    )
    gold_pnl_pct = (
        ((gold_now / gold_avg) - 1) * 100
        if (gold_now and gold_avg > 0 and gold_grams > 0) else None
    )

    # 总浮盈 % = (现市值 - 总成本) / 总成本，现金不算成本/收益
    ndq_cost_cny = ndq_avg * ndq_shares * audcny if ndq_avg > 0 else 0
    ndq_value_cny = (ndq_price or 0) * ndq_shares * audcny if ndq_price else ndq_cost_cny
    gold_cost_cny = gold_avg * gold_grams if gold_avg > 0 else 0
    gold_value_cny = (gold_now or 0) * gold_grams if gold_now else gold_cost_cny
    total_cost = ndq_cost_cny + gold_cost_cny
    total_value = ndq_value_cny + gold_value_cny
    total_pnl_pct = ((total_value / total_cost) - 1) * 100 if total_cost > 0 else 0.0

    return Snapshot(
        ts=datetime.now().astimezone().isoformat(timespec="seconds"),
        total_pnl_pct=round(total_pnl_pct, 4),
        ndq_pnl_pct=round(ndq_pnl_pct, 4) if ndq_pnl_pct is not None else None,
        gold_pnl_pct=round(gold_pnl_pct, 4) if gold_pnl_pct is not None else None,
    )


def _append_history(snap: Snapshot) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": snap.ts,
            "total_pnl_pct": snap.total_pnl_pct,
            "ndq_pnl_pct": snap.ndq_pnl_pct,
            "gold_pnl_pct": snap.gold_pnl_pct,
        }, ensure_ascii=False) + "\n")


def _read_history(window_days: int = WINDOW_DAYS) -> List[Dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    cutoff = datetime.now().astimezone() - timedelta(days=window_days)
    out: List[Dict[str, Any]] = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = datetime.fromisoformat(entry["ts"])
                if ts >= cutoff:
                    out.append(entry)
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
    return out


def _ensure_benchmarks_fresh(start_date: str, end_date: str) -> None:
    """Auto-refresh benchmark caches that don't cover [start_date, end_date].

    Without this, if cached benchmark data ends before the PnL history window
    starts, ``to_pct_series`` returns empty → all benchmark bars disappear.
    Only refreshes stale entries; already-fresh caches are left untouched.
    Network failures are swallowed per-key (same as refresh_benchmark design).
    """
    for key in BENCHMARKS:
        cached = load_benchmark(key)
        if cached and cached.get("end", "") >= start_date:
            continue  # cache covers the window
        try:
            refresh_benchmark(key, start_date, end_date)
        except Exception as exc:
            log.warning("benchmark refresh failed for %s: %s", key, exc)


def render_svg(history: List[Dict[str, Any]]) -> str:
    """IO 包装：刷基准缓存 + 取基准序列，再委托 pnl_render 纯渲染。

    _ensure_benchmarks_fresh / get_all_series 在本命名空间解析——
    tests/test_pnl_snapshot 的 monkeypatch 依赖这一点。
    """
    if history:
        start_date = history[0]["ts"][:10]
        end_date = history[-1]["ts"][:10]
        # 自动刷新过期基准缓存 — 缓存数据不覆盖当前渲染窗口时重拉
        # （openInvest#92 根因之一：缓存 Apr 27 结束，窗口从 May 28 开始 → 0 条基准）
        _ensure_benchmarks_fresh(start_date, end_date)
        benchmark_series = get_all_series(start_date)
    else:
        benchmark_series = []
    return _render_svg_pure(history, benchmark_series)


def _is_trading_window(now: Optional[datetime] = None) -> bool:
    """IO 包装：now 未传取当前时钟，委托 pnl_render 纯核（口径 docstring 见彼处）。"""
    from datetime import timezone
    return _is_trading_window_pure(now if now is not None else datetime.now(timezone.utc))


def _outperform_events(snap: Snapshot) -> List[Dict[str, Any]]:
    """IO 包装：读 history + 拉基准序列，再委托 pnl_render 纯核。

    注意 get_all_series() 这个调用与上游签名不符（缺 start_date），TypeError
    会被 except 吞掉 → 事件恒为空。这是搬迁前就存在的存量行为，纯搬迁不改——
    修复跟踪见 GitHub issue（outperform feed 静默失效）。
    """
    if snap.total_pnl_pct is None:
        return []
    history = _read_history(window_days=WINDOW_DAYS)
    if not history:
        return []
    try:
        all_series = get_all_series()
    except Exception as e:
        log.warning(f"benchmark series 拉失败，跳过 outperform 事件: {e}")
        return []
    return _outperform_events_pure(snap, history, all_series)



def _redact_token_in(text: str) -> str:
    """脱敏 git stderr 里可能出现的 'https://x-access-token:gho_xxx@github.com/...'
    避免 GITHUB_TOKEN 流到 scheduler 日志（audit security M1）"""
    import re as _re
    return _re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", text)


def _auto_push_svg() -> Dict[str, Any]:
    """可选：把 docs/pnl_chart.svg commit 到 git 并 push 到 GitHub。

    只在 INVEST_PNL_AUTOPUSH=1 时启用。token 从 GITHUB_TOKEN env 读。
    任何失败都吞掉只 print，避免 PnL 数据已落盘但 push 失败导致整个 job 标 fail。

    分支策略：
    - INVEST_PNL_PUSH_BRANCH=main (默认): 直接推主分支，git log 会有 hourly 噪音
    - INVEST_PNL_PUSH_BRANCH=pnl-data: 推到独立 orphan 分支（每次 reset 到只
      含最新 SVG），主分支历史保持干净
    """
    if os.getenv("INVEST_PNL_AUTOPUSH", "0") != "1":
        return {"pushed": False, "reason": "INVEST_PNL_AUTOPUSH != 1"}

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        return {"pushed": False, "reason": "GITHUB_TOKEN env 缺失"}

    branch = os.getenv("INVEST_PNL_PUSH_BRANCH", "main").strip() or "main"
    use_orphan = (branch != "main")

    def _git(args: List[str], check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(ROOT), capture_output=True, text=True,
            check=check,
        )

    try:
        # 拿 remote URL，注入 token 走 https
        remote = _git(["config", "--get", "remote.origin.url"]).stdout.strip()
        if not remote.startswith("https://github.com/"):
            return {"pushed": False, "reason": f"only https github remote supported, got {remote}"}
        # https://github.com/owner/repo.git → https://x-access-token:TOKEN@github.com/owner/repo.git
        authed_remote = remote.replace(
            "https://", f"https://x-access-token:{token}@", 1
        )

        if use_orphan:
            # Orphan 分支模式：临时 worktree 切到 pnl-data，只 commit SVG，force push
            import tempfile
            with tempfile.TemporaryDirectory() as wt_dir:
                # 检查远端有没有这个分支
                ls = _git(["ls-remote", "--heads", authed_remote, branch], check=False)
                exists_remote = bool(ls.stdout.strip())
                if exists_remote:
                    _git(["worktree", "add", wt_dir, "-B", branch,
                          f"refs/remotes/origin/{branch}"], check=False)
                else:
                    # 全新 orphan：先 worktree add 主分支占位，然后切到 orphan
                    _git(["worktree", "add", "--detach", wt_dir, "HEAD"])

                wt = Path(wt_dir)
                if not exists_remote:
                    subprocess.run(["git", "checkout", "--orphan", branch],
                                   cwd=str(wt), check=True, capture_output=True)
                    subprocess.run(["git", "rm", "-rf", "--cached", "."],
                                   cwd=str(wt), check=False, capture_output=True)
                    # 清空 worktree 但保留 .git
                    for p in wt.iterdir():
                        if p.name != ".git":
                            if p.is_dir():
                                import shutil
                                shutil.rmtree(p)
                            else:
                                p.unlink()

                # 复制最新 SVG 进 worktree 并 commit
                target_svg = wt / "docs" / "pnl_chart.svg"
                target_svg.parent.mkdir(parents=True, exist_ok=True)
                target_svg.write_bytes(SVG_PATH.read_bytes())
                # README 提示
                (wt / "README.md").write_text(
                    "# pnl-data branch\n\n"
                    "This orphan branch holds the auto-generated PnL chart only. "
                    "Do not commit code here. Updated hourly by `jobs/pnl_snapshot`.\n",
                    encoding="utf-8",
                )

                subprocess.run(["git", "add", "docs/pnl_chart.svg", "README.md"],
                               cwd=str(wt), check=True, capture_output=True)
                # 没变化跳过
                diff = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                       cwd=str(wt), capture_output=True)
                if diff.returncode == 0:
                    _git(["worktree", "remove", "--force", wt_dir], check=False)
                    return {"pushed": False, "reason": "no svg change", "branch": branch}

                subprocess.run([
                    "git", "-c", "user.name=pnl-bot",
                    "-c", "user.email=pnl-bot@invest.local",
                    "commit", "-m", "chore(pnl): hourly snapshot [skip ci]",
                ], cwd=str(wt), check=True, capture_output=True)

                # Orphan 分支总是 force push（每次 reset 到最新）
                push = subprocess.run(
                    ["git", "push", "--force", authed_remote, f"HEAD:{branch}"],
                    cwd=str(wt), capture_output=True, text=True,
                )
                _git(["worktree", "remove", "--force", wt_dir], check=False)
                if push.returncode != 0:
                    return {"pushed": False, "reason": f"push failed: {_redact_token_in(push.stderr[:200])}",
                            "branch": branch}
                return {"pushed": True, "branch": branch, "mode": "orphan"}

        # 主分支模式：直接 add + commit + push
        _git(["add", "docs/pnl_chart.svg"])
        diff = _git(["diff", "--cached", "--quiet"], check=False)
        if diff.returncode == 0:
            return {"pushed": False, "reason": "no svg change", "branch": "main"}
        _git([
            "-c", "user.name=pnl-bot",
            "-c", "user.email=pnl-bot@invest.local",
            "commit", "-m", "chore(pnl): hourly snapshot [skip ci]",
        ])
        push = _git(["push", authed_remote, f"HEAD:{branch}"], check=False)
        if push.returncode != 0:
            # 必须脱敏：git push 失败 stderr 会回显带 token 的 authed_remote
            # （'fatal: unable to access https://x-access-token:TOKEN@github.com/...'）
            # 与 orphan 路径同口径，避免 GITHUB_TOKEN 流到 scheduler 日志（audit security M1）
            return {"pushed": False,
                    "reason": f"push failed: {_redact_token_in(push.stderr[:200])}",
                    "branch": branch}
        return {"pushed": True, "branch": branch, "mode": "main"}

    except subprocess.CalledProcessError as e:
        # e.stderr 同样可能带 authed_remote（token），统一脱敏
        raw = e.stderr[:200] if e.stderr else str(e)
        return {"pushed": False, "reason": f"git failure: {_redact_token_in(raw)}"}
    except Exception as e:
        # 兜底分支同样可能带 authed_remote（token）—— 非 CalledProcessError 的
        # subprocess 异常（OSError/TimeoutExpired）或库异常的 message 里也会回显
        # 带 token 的 URL，统一脱敏。type 名不含 secret，保留不脱敏。
        return {"pushed": False, "reason": f"unexpected: {type(e).__name__}: {_redact_token_in(str(e))}"}


def _persist_outperform(events: List[Dict[str, Any]]) -> None:
    """事件落盘 → docs/outperform_events.jsonl（append-only）+ 同步刷 README marker

    README hero 区有 `<!-- OUTPERFORM_FEED_START --> ... <!-- OUTPERFORM_FEED_END -->`
    两个 marker，本函数会把最近 3 条事件渲染成 markdown bullet 写进中间。这样
    pnl-data force-push 时 GitHub README 自动展示最新跑赢瞬间——PM-Growth 增长杠杆。
    """
    if not events:
        return
    out_path = SVG_PATH.parent / "outperform_events.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    # 同步刷 README marker 区域
    try:
        _update_readme_outperform_feed(out_path)
    except Exception as e:  # noqa: BLE001  README 刷新失败不阻断主流程
        log.warning(f"README outperform feed 刷新失败（不影响 jsonl 落盘）: {e}")


_CANONICAL_OWNER = "longsizhuo"


def _outperform_feed_attribution() -> Tuple[str, str, str]:
    """据 git remote 推断 README outperform feed 的署名 + 链接 + 分支。

    fork / 自托管用户的 README 不该挂"作者账户"+ 指向作者仓库的链接——数据是
    他们自己的，归属也该是他们自己的。从 remote.origin.url 解析 owner/repo：
      - owner == 作者     → ("作者账户", 作者仓 tree 链接, 分支)  原行为不变
      - 其它 owner        → ("本账户", 该 fork 自己的 tree 链接, 分支)
      - 解析不到 remote   → ("本账户", "", 分支)  纯文字，不外链任何人
    """
    owner = repo = ""
    try:
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        ).stdout.strip()
        # host 前必须是 行首 / @ / /（挡掉 my-github.com 这类子串误匹配）；
        # 尾部容忍 .git 和 trailing /（否则 https://…/openInvest/ 会漏判）
        m = re.search(r"(?:^|[@/])github\.com[:/]+([^/]+?)/([^/]+?)(?:\.git)?/?$", remote)
        if m:
            owner, repo = m.group(1), m.group(2)
    except Exception:  # noqa: BLE001  推断失败退化成无链接，不阻断 README 刷新
        pass

    branch = os.getenv("INVEST_PNL_PUSH_BRANCH", "pnl-data").strip() or "pnl-data"
    label = "作者账户" if owner.lower() == _CANONICAL_OWNER else "本账户"
    link = f"https://github.com/{owner}/{repo}/tree/{branch}" if owner and repo else ""
    return label, link, branch


def _update_readme_outperform_feed(jsonl_path: Path, top_n: int = 3) -> None:
    """读 outperform_events.jsonl 最新 N 条，渲染 markdown 写进 README marker 之间。

    README marker：
      <!-- OUTPERFORM_FEED_START -->
      （内容由本函数自动生成）
      <!-- OUTPERFORM_FEED_END -->
    """
    readme = SVG_PATH.parent.parent / "README.md"
    if not readme.exists():
        return
    if not jsonl_path.exists():
        return

    # 取最后 N 条事件
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    recent: List[Dict[str, Any]] = []
    for line in lines[-200:]:
        line = line.strip()
        if not line:
            continue
        try:
            recent.append(json.loads(line))
        except Exception:
            continue
    if not recent:
        return
    recent.sort(key=lambda e: e.get("ts", ""), reverse=True)
    # 同基准只保留最新一条（避免列表全是"跑赢余额宝"5 次）
    seen_bench: set = set()
    deduped: List[Dict[str, Any]] = []
    for ev in recent:
        bench = ev.get("benchmark", "")
        if bench in seen_bench:
            continue
        seen_bench.add(bench)
        deduped.append(ev)
        if len(deduped) >= top_n:
            break

    # 金融视角红线：固定免责 + 展示 winning + losing 两类事件，避免 survivorship 偏差
    # 署名/链接按 git remote 推断——fork / 自托管用户的 README 不该挂"作者账户"+作者仓链接
    feed_label, feed_link, feed_branch = _outperform_feed_attribution()
    refresh = (f"由 [{feed_branch} 分支]({feed_link}) 每 2h 自动刷新"
               if feed_link else "每 2h 自动刷新")
    rendered = [
        f"> 📈 **{feed_label}实盘事件**（最近 vs 基准，{refresh}）：",
        ">",
    ]
    for ev in deduped:
        ts = str(ev.get("ts", ""))[:10]
        label = ev.get("label", "")
        # win/loss 用不同 emoji 区分，避免视觉只看到"赢"
        marker = "🟢" if ev.get("is_outperform") else "🔴"
        rendered.append(f"> - {marker} `{ts}` {label}")
    rendered.append(">")
    if feed_label == "作者账户":
        rendered.append(
            "> *以上为作者本人账户历史事件，仅供工具效果参考，**不构成投资建议**，"
            "过去表现不预示未来收益。fork 用户的部署会看到自己的事件。*",
        )
    else:
        rendered.append(
            "> *以上为本部署账户历史事件，仅供工具效果参考，**不构成投资建议**，"
            "过去表现不预示未来收益。*",
        )

    new_block = "\n".join(rendered)
    text = readme.read_text(encoding="utf-8")
    start_marker = "<!-- OUTPERFORM_FEED_START"
    end_marker = "<!-- OUTPERFORM_FEED_END -->"
    s_idx = text.find(start_marker)
    e_idx = text.find(end_marker)
    if s_idx == -1 or e_idx == -1 or e_idx < s_idx:
        # marker 不存在则跳过（fork 用户可能删了 hero 区）
        return
    # 找到 START 行结尾
    s_line_end = text.find("\n", s_idx)
    if s_line_end == -1:
        return
    before = text[: s_line_end + 1]
    after = text[e_idx:]
    new_text = before + new_block + "\n" + after
    readme.write_text(new_text, encoding="utf-8")


def run() -> Dict[str, Any]:
    """job entry：算快照 + 写历史 + 渲染 SVG + 可选自动 push"""
    # 跳过非交易时段（周末 / 凌晨）
    if not _is_trading_window():
        return {"status": "skipped", "reason": "non_trading_window",
                "now": datetime.now().isoformat(timespec="seconds")}

    store = MemoryStore()
    snap = _compute_snapshot(store)
    if snap is None:
        return {"status": "skipped", "reason": "no_portfolio"}

    _append_history(snap)
    history = _read_history()

    # 渲染并原子写入 SVG
    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    svg_content = render_svg(history)
    tmp = SVG_PATH.with_suffix(".svg.tmp")
    tmp.write_text(svg_content, encoding="utf-8")
    tmp.replace(SVG_PATH)

    # 跑赢基准的"可分享瞬间"事件（PM-3 增长杠杆）
    events = _outperform_events(snap)
    _persist_outperform(events)

    # 可选：commit + push 到 GitHub（受 INVEST_PNL_AUTOPUSH env 控制）
    push_result = _auto_push_svg()

    return {
        "status": "ok",
        "ts": snap.ts,
        "history_points": len(history),
        "svg_path": str(SVG_PATH),
        # **故意不在 return 里暴露百分比数字**，避免 scheduler 日志泄露
        "trend": "up" if snap.total_pnl_pct > 0 else (
            "down" if snap.total_pnl_pct < 0 else "flat"
        ),
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
    history = _read_history()
    SVG_PATH.parent.mkdir(parents=True, exist_ok=True)
    svg_content = render_svg(history)
    tmp = SVG_PATH.with_suffix(".svg.tmp")
    tmp.write_text(svg_content, encoding="utf-8")
    tmp.replace(SVG_PATH)
    push_result = _auto_push_svg()
    return {
        "status": "ok_render_only",
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
