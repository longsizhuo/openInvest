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
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from core.benchmarks import BenchmarkSeries, get_all_series
from core.memory_store import MemoryStore
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot

load_dotenv()

ROOT = Path(__file__).parent.parent
HISTORY_PATH = ROOT / "memory" / ".state" / "pnl_history.jsonl"
SVG_PATH = ROOT / "docs" / "pnl_chart.svg"

# SVG 画布尺寸
W, H = 800, 280
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 50, 30, 30, 30
PLOT_W = W - MARGIN_L - MARGIN_R
PLOT_H = H - MARGIN_T - MARGIN_B

# 时间窗：图上只展示最近 30 天
WINDOW_DAYS = 30


@dataclass
class Snapshot:
    ts: str
    total_pnl_pct: float
    ndq_pnl_pct: Optional[float]
    gold_pnl_pct: Optional[float]


def _safe_close(symbol: str) -> Optional[float]:
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def _compute_snapshot(store: MemoryStore) -> Optional[Snapshot]:
    portfolio = store.read("portfolio")
    if portfolio is None:
        return None

    cash_cny = float(portfolio.get("cash_cny", 0))
    aud_cash = float(portfolio.get("aud_cash", 0))
    ndq_shares = float(portfolio.get("ndq_shares", 0))
    ndq_avg = float(portfolio.get("ndq_avg_cost_aud_per_share", 0) or 0)
    gold_grams = float(portfolio.get("gold_grams", 0))
    gold_avg = float(portfolio.get("gold_avg_cost_cny_per_gram", 0) or 0)

    audcny = _safe_close("AUDCNY=X") or 4.7
    ndq_price = _safe_close("NDQ.AX")
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_now = snap.spot_cny_per_gram if snap else None

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


def _project_y(value: float, vmin: float, vmax: float) -> float:
    """把百分比映射到 SVG y 坐标（越大越往上）"""
    if vmax == vmin:
        return MARGIN_T + PLOT_H / 2
    norm = (value - vmin) / (vmax - vmin)
    return MARGIN_T + (1 - norm) * PLOT_H


def _series_polyline(
    history: List[Dict[str, Any]], key: str, vmin: float, vmax: float
) -> str:
    """把一条 series 转成 SVG polyline 的 points 字符串"""
    n = len(history)
    if n == 0:
        return ""
    pts: List[str] = []
    for i, entry in enumerate(history):
        v = entry.get(key)
        if v is None:
            continue
        x = MARGIN_L + (PLOT_W * i / max(n - 1, 1))
        y = _project_y(v, vmin, vmax)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _benchmark_polyline(
    series: BenchmarkSeries, history: List[Dict[str, Any]],
    vmin: float, vmax: float,
) -> str:
    """把 BenchmarkSeries 的 {date: pct} 投影到 history 时间轴上的 SVG points。

    history 里每条记录有 ts（用户实盘的时间戳），找最近的基准日期 → 取那天的 pct。
    """
    if not history or not series.points:
        return ""
    pts: List[str] = []
    n = len(history)
    sorted_bench_dates = sorted(series.points.keys())
    for i, entry in enumerate(history):
        # 用户 history 的 ts 是 ISO，截前 10 位拿日期
        date_str = entry["ts"][:10]
        # 找最近的（不晚于）基准日期
        candidates = [d for d in sorted_bench_dates if d <= date_str]
        if not candidates:
            continue
        v = series.points[candidates[-1]]
        x = MARGIN_L + (PLOT_W * i / max(n - 1, 1))
        y = _project_y(v, vmin, vmax)
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def render_svg(history: List[Dict[str, Any]]) -> str:
    """渲染折线图。**故意不写任何数字标签**，只显示线条、0% 基线、方向箭头。

    叠加 vs 基准：从 core/benchmarks 加载所有缓存的 series（沪深 300 / 公募基金 /
    余额宝 / Wealthfront 等），按用户线的时间轴投影同图叠加。
    用户线（Total / NDQ / Gold）保持粗实线 + 高对比；基准线细 + 半透明。
    """
    if not history:
        # 空状态：一句话占位
        return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-label="PnL trend chart (no data yet)">
  <rect width="{W}" height="{H}" fill="#0d1117"/>
  <text x="{W//2}" y="{H//2}" text-anchor="middle" fill="#8b949e" font-family="ui-monospace, monospace" font-size="14">
    [PnL chart — 数据采集中，请等待 jobs/pnl_snapshot 跑几次后查看]
  </text>
</svg>
"""

    # 加载所有基准 series（用 history 起始日当 baseline）
    start_date = history[0]["ts"][:10]
    benchmark_series = get_all_series(start_date)

    # 算 y 轴范围（含基准）
    all_values: List[float] = []
    for entry in history:
        for k in ("total_pnl_pct", "ndq_pnl_pct", "gold_pnl_pct"):
            v = entry.get(k)
            if v is not None:
                all_values.append(v)
    for s in benchmark_series:
        # 只考虑窗口内（>= start_date）的基准点
        for d, v in s.points.items():
            if d >= start_date:
                all_values.append(v)
    all_values.append(0.0)  # 0% 基线必在范围内
    vmin, vmax = min(all_values), max(all_values)
    pad = max((vmax - vmin) * 0.1, 0.5)
    vmin -= pad
    vmax += pad

    zero_y = _project_y(0.0, vmin, vmax)

    total_line = _series_polyline(history, "total_pnl_pct", vmin, vmax)
    ndq_line = _series_polyline(history, "ndq_pnl_pct", vmin, vmax)
    gold_line = _series_polyline(history, "gold_pnl_pct", vmin, vmax)

    # 最新点的趋势方向：仅在末尾画一个上箭头/下箭头表示当前趋势，但不写数值
    latest_total = next(
        (entry.get("total_pnl_pct") for entry in reversed(history)
         if entry.get("total_pnl_pct") is not None),
        0.0,
    )
    arrow = "▲" if latest_total > 0 else ("▼" if latest_total < 0 else "■")
    arrow_color = "#3fb950" if latest_total > 0 else ("#f85149" if latest_total < 0 else "#8b949e")

    # 渲染基准线（细 + 半透明，不抢用户线视觉焦点）
    bench_polylines = []
    for s in benchmark_series:
        pts = _benchmark_polyline(s, history, vmin, vmax)
        if pts:
            bench_polylines.append(
                f'<polyline points="{pts}" fill="none" stroke="{s.color}" '
                f'stroke-width="1" stroke-dasharray="{s.dash}" opacity="0.45"/>'
            )

    # 图例（基准分组，每组一行）—— SVG 高度从 280 拉到 380 留出图例空间
    legend_lines: List[str] = []
    legend_y = MARGIN_T + 12
    # 用户线（最显眼）
    legend_lines.append(
        f'<g transform="translate({MARGIN_L + 8}, {legend_y})" class="label">'
        f'<line x1="0" y1="0" x2="14" y2="0" stroke="#d29922" stroke-width="2.5"/>'
        f'<text x="20" y="4" fill="#c9d1d9" font-weight="bold">我的实盘 Total</text>'
        f'<line x1="120" y1="0" x2="134" y2="0" stroke="#58a6ff" stroke-width="1.5"/>'
        f'<text x="140" y="4" fill="#c9d1d9">NDQ.AX</text>'
        f'<line x1="200" y1="0" x2="214" y2="0" stroke="#f0a500" stroke-width="1.5"/>'
        f'<text x="220" y="4" fill="#c9d1d9">Gold</text>'
        f'</g>'
    )
    # 基准按 group 分组列出
    grouped: Dict[str, List[BenchmarkSeries]] = {}
    for s in benchmark_series:
        grouped.setdefault(s.group, []).append(s)
    group_titles = {
        "index": "📊 大盘指数",
        "fund": "🏦 公募基金",
        "savings": "💰 银行 / 货币基金",
        "ai_advisor": "🤖 AI 投顾基准",
    }
    legend_y = H - 70
    for group_key in ["index", "fund", "savings", "ai_advisor"]:
        if group_key not in grouped:
            continue
        items = grouped[group_key]
        legend_lines.append(
            f'<text x="{MARGIN_L + 8}" y="{legend_y}" fill="#8b949e" class="label">'
            f'{group_titles[group_key]}:</text>'
        )
        x = MARGIN_L + 130
        for s in items:
            legend_lines.append(
                f'<line x1="{x}" y1="{legend_y - 4}" x2="{x + 12}" y2="{legend_y - 4}" '
                f'stroke="{s.color}" stroke-width="1.5" stroke-dasharray="{s.dash}" opacity="0.7"/>'
            )
            legend_lines.append(
                f'<text x="{x + 16}" y="{legend_y}" fill="#c9d1d9" class="label" font-size="10">'
                f'{s.key}</text>'
            )
            x += max(110, len(s.key) * 7 + 30)
        legend_y += 14

    # SVG 高度自适应（基准多了图例占地多）
    H_DYNAMIC = max(H, MARGIN_T + PLOT_H + 80 + 14 * len(grouped))
    new_zero_y = zero_y  # 不需重算，因为图区高度 PLOT_H 不变

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H_DYNAMIC}" role="img" aria-label="PnL trend chart with benchmarks (privacy-preserving, no absolute numbers)">
  <style>
    .label {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }}
  </style>
  <!-- 背景 -->
  <rect width="{W}" height="{H_DYNAMIC}" fill="#0d1117"/>
  <!-- 0% 基线 -->
  <line x1="{MARGIN_L}" y1="{new_zero_y:.1f}" x2="{W - MARGIN_R}" y2="{new_zero_y:.1f}"
        stroke="#30363d" stroke-width="1" stroke-dasharray="4 4"/>
  <text x="{MARGIN_L - 6}" y="{new_zero_y + 4:.1f}" text-anchor="end" fill="#6e7681" class="label">0%</text>
  <text x="{MARGIN_L - 6}" y="{MARGIN_T + 10}" text-anchor="end" fill="#3fb950" class="label">+</text>
  <text x="{MARGIN_L - 6}" y="{MARGIN_T + PLOT_H - 2}" text-anchor="end" fill="#f85149" class="label">−</text>
  <text x="{MARGIN_L}" y="{MARGIN_T + PLOT_H + 18}" fill="#6e7681" class="label">30 天前</text>
  <text x="{W - MARGIN_R}" y="{MARGIN_T + PLOT_H + 18}" text-anchor="end" fill="#6e7681" class="label">今天</text>

  <!-- 基准线（先画，避免遮挡用户线）-->
  {chr(10).join(bench_polylines)}

  <!-- 用户实盘三线（粗实线，高对比）-->
  {f'<polyline points="{ndq_line}" fill="none" stroke="#58a6ff" stroke-width="1.5" opacity="0.85"/>' if ndq_line else ''}
  {f'<polyline points="{gold_line}" fill="none" stroke="#f0a500" stroke-width="1.5" opacity="0.85"/>' if gold_line else ''}
  {f'<polyline points="{total_line}" fill="none" stroke="#d29922" stroke-width="2.5"/>' if total_line else ''}

  <!-- 当前趋势箭头 -->
  <text x="{W - MARGIN_R - 10}" y="{MARGIN_T + 18}" text-anchor="end" fill="{arrow_color}" font-size="22" font-weight="bold">{arrow}</text>

  <!-- 图例 -->
  {chr(10).join(legend_lines)}
</svg>
"""


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
                    return {"pushed": False, "reason": f"push failed: {push.stderr[:200]}",
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
            return {"pushed": False, "reason": f"push failed: {push.stderr[:200]}",
                    "branch": branch}
        return {"pushed": True, "branch": branch, "mode": "main"}

    except subprocess.CalledProcessError as e:
        return {"pushed": False, "reason": f"git failure: {e.stderr[:200] if e.stderr else e}"}
    except Exception as e:
        return {"pushed": False, "reason": f"unexpected: {type(e).__name__}: {e}"}


def run() -> Dict[str, Any]:
    """job entry：算快照 + 写历史 + 渲染 SVG + 可选自动 push"""
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
        "push": push_result,
    }


if __name__ == "__main__":
    print(run())
