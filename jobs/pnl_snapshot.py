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
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

from core.benchmarks import BenchmarkSeries, get_all_series
from core.memory_store import MemoryStore
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot

load_dotenv()

ROOT = Path(__file__).parent.parent
HISTORY_PATH = ROOT / "memory" / ".state" / "pnl_history.jsonl"
SVG_PATH = ROOT / "docs" / "pnl_chart.svg"

# SVG 画布：上半部分折线图 + 下半部分横向柱状图
W = 800
LINE_H = 240        # 上半折线图区域高度
BAR_ROW_H = 22      # 每条 bar 的高度
BAR_TOP_PAD = 50    # 柱状图区上方留给标题
BAR_BOTTOM_PAD = 30
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 50, 30, 30, 30
PLOT_W = W - MARGIN_L - MARGIN_R
PLOT_H = LINE_H - MARGIN_T - MARGIN_B

# 时间窗：图上只展示最近 30 天
WINDOW_DAYS = 30


@dataclass
class Snapshot:
    ts: str
    total_pnl_pct: float
    ndq_pnl_pct: Optional[float]
    gold_pnl_pct: Optional[float]


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


def _latest_pct(series: BenchmarkSeries, start_date: str) -> Optional[float]:
    """基准 series 截至最新的累计涨幅 % (相对 start_date)"""
    if not series.points:
        return None
    valid = [(d, v) for d, v in series.points.items() if d >= start_date]
    if not valid:
        return None
    valid.sort()
    return valid[-1][1]


def render_svg(history: List[Dict[str, Any]]) -> str:
    """上半部分：用户三线折线趋势 (Total / NDQ / Gold)
       下半部分：横向柱状图，11 个基准 + 用户实盘按累计涨幅排序

    柱状图灵感：类似 LLM benchmark (MMLU / HellaSwag) 的对比图。基准的"累计涨幅"
    与时间轴弱相关，柱子高度 (= % 涨幅) 一目了然，比折线叠加更直观。
    用户实盘柱用粗黄色 + ★ 标识突出，基准柱按色系分组。
    """
    if not history:
        return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} 100" role="img" aria-label="PnL chart (no data yet)">
  <rect width="{W}" height="100" fill="#0d1117"/>
  <text x="{W//2}" y="55" text-anchor="middle" fill="#8b949e" font-family="ui-monospace, monospace" font-size="14">
    [PnL chart — 数据采集中，请等待 jobs/pnl_snapshot 跑几次后查看]
  </text>
</svg>
"""

    start_date = history[0]["ts"][:10]
    benchmark_series = get_all_series(start_date)

    # ===== 上半：用户三线折线（不再叠加基准）=====
    user_values: List[float] = []
    for entry in history:
        for k in ("total_pnl_pct", "ndq_pnl_pct", "gold_pnl_pct"):
            v = entry.get(k)
            if v is not None:
                user_values.append(v)
    user_values.append(0.0)
    vmin, vmax = min(user_values), max(user_values)
    pad = max((vmax - vmin) * 0.15, 0.5)
    vmin -= pad
    vmax += pad
    zero_y = _project_y(0.0, vmin, vmax)

    total_line = _series_polyline(history, "total_pnl_pct", vmin, vmax)
    ndq_line = _series_polyline(history, "ndq_pnl_pct", vmin, vmax)
    gold_line = _series_polyline(history, "gold_pnl_pct", vmin, vmax)

    # latest_total 永远是 float（next 的 default 是 0.0），但 mypy 看不出来：
    # 显式 cast 让类型检查器满意，也避免后续 > / < 比较的 None 风险（audit eng M8）
    _lt = next(
        (e.get("total_pnl_pct") for e in reversed(history)
         if e.get("total_pnl_pct") is not None),
        0.0,
    )
    latest_total: float = float(_lt) if _lt is not None else 0.0
    arrow = "▲" if latest_total > 0 else ("▼" if latest_total < 0 else "■")
    arrow_color = "#3fb950" if latest_total > 0 else ("#f85149" if latest_total < 0 else "#8b949e")

    # ===== 下半：横向柱状图 =====
    # 收集所有数据点：(label, pct, color, is_user)
    bars: List[Tuple[str, float, str, bool]] = []
    for s in benchmark_series:
        pct = _latest_pct(s, start_date)
        if pct is None:
            continue
        bars.append((s.key, pct, s.color, False))
    # 用户实盘 Total 加进去（粗黄色 + ★ 标识）
    bars.append((f"★ 我的实盘", latest_total, "#d29922", True))

    # 按 % 降序排列
    bars.sort(key=lambda x: x[1], reverse=True)

    # 算柱状图 X 轴范围
    bar_pcts = [b[1] for b in bars]
    bar_max = max(max(bar_pcts), 0.5)
    bar_min = min(min(bar_pcts), -0.5)
    bar_range = bar_max - bar_min
    # 0% 在柱状图中的 x 坐标
    BAR_AXIS_LEFT = 200    # 左侧给 label 留空间
    BAR_AXIS_RIGHT = W - 80  # 右侧给百分比数字留空间
    BAR_AXIS_W = BAR_AXIS_RIGHT - BAR_AXIS_LEFT
    if bar_range == 0:
        zero_x = BAR_AXIS_LEFT + BAR_AXIS_W / 2
    else:
        zero_x = BAR_AXIS_LEFT + BAR_AXIS_W * (-bar_min / bar_range)

    bar_y_start = LINE_H + BAR_TOP_PAD
    bar_svg: List[str] = []
    for i, (label, pct, color, is_user) in enumerate(bars):
        y = bar_y_start + i * BAR_ROW_H
        # 柱条 x 起点和宽度
        if pct >= 0:
            bar_x = zero_x
            bar_w = (pct / bar_max) * (BAR_AXIS_RIGHT - zero_x) if bar_max > 0 else 0
        else:
            bar_w = (abs(pct) / abs(bar_min)) * (zero_x - BAR_AXIS_LEFT) if bar_min < 0 else 0
            bar_x = zero_x - bar_w

        # label（左侧）
        label_color = "#d29922" if is_user else "#c9d1d9"
        label_weight = "bold" if is_user else "normal"
        bar_svg.append(
            f'<text x="{BAR_AXIS_LEFT - 8}" y="{y + BAR_ROW_H / 2 + 4:.1f}" '
            f'text-anchor="end" fill="{label_color}" class="label" font-weight="{label_weight}">{label}</text>'
        )
        # bar 矩形（用户柱粗 + 不透明，基准柱半透明）
        rect_h = BAR_ROW_H - 6
        opacity = "1" if is_user else "0.7"
        bar_svg.append(
            f'<rect x="{bar_x:.1f}" y="{y + 3}" width="{bar_w:.1f}" height="{rect_h}" '
            f'fill="{color}" opacity="{opacity}" rx="2"/>'
        )
        # 百分比数字（产品对比榜单语境下，用户柱也直接显示真实 %，
        # 因为 % 是相对量、不暴露资产规模，与基准并排时藏起来反而显得心虚）
        pct_text = f"{pct:+.2f}%"
        pct_x = (bar_x + bar_w + 6) if pct >= 0 else (bar_x - 6)
        pct_anchor = "start" if pct >= 0 else "end"
        bar_svg.append(
            f'<text x="{pct_x:.1f}" y="{y + BAR_ROW_H / 2 + 4:.1f}" '
            f'text-anchor="{pct_anchor}" fill="{color}" class="label" '
            f'font-weight="{label_weight}">{pct_text}</text>'
        )

    # 0 线（贯穿整个柱状图区）
    bar_y_end = bar_y_start + len(bars) * BAR_ROW_H
    zero_line_svg = (
        f'<line x1="{zero_x:.1f}" y1="{bar_y_start - 5}" '
        f'x2="{zero_x:.1f}" y2="{bar_y_end + 5}" '
        f'stroke="#30363d" stroke-width="1" stroke-dasharray="3 3"/>'
        f'<text x="{zero_x:.1f}" y="{bar_y_start - 8}" text-anchor="middle" '
        f'fill="#6e7681" class="label">0%</text>'
    )

    # SVG 总高度
    H_TOTAL = bar_y_end + BAR_BOTTOM_PAD

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H_TOTAL}" role="img" aria-label="PnL chart with benchmark bars">
  <style>
    .label {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }}
    .title {{ font-family: -apple-system, "Segoe UI", sans-serif; font-size: 13px; font-weight: bold; }}
  </style>
  <rect width="{W}" height="{H_TOTAL}" fill="#0d1117"/>

  <!-- ===== 上半：折线图（用户三线趋势）===== -->
  <text x="{MARGIN_L}" y="20" fill="#c9d1d9" class="title">📈 实盘 PnL 趋势 (最近 {WINDOW_DAYS} 天)</text>
  <line x1="{MARGIN_L}" y1="{zero_y:.1f}" x2="{W - MARGIN_R}" y2="{zero_y:.1f}"
        stroke="#30363d" stroke-width="1" stroke-dasharray="4 4"/>
  <text x="{MARGIN_L - 6}" y="{zero_y + 4:.1f}" text-anchor="end" fill="#6e7681" class="label">0%</text>
  <text x="{MARGIN_L - 6}" y="{MARGIN_T + 10}" text-anchor="end" fill="#3fb950" class="label">+</text>
  <text x="{MARGIN_L - 6}" y="{MARGIN_T + PLOT_H - 2}" text-anchor="end" fill="#f85149" class="label">−</text>
  <text x="{MARGIN_L}" y="{MARGIN_T + PLOT_H + 18}" fill="#6e7681" class="label">{WINDOW_DAYS} 天前</text>
  <text x="{W - MARGIN_R}" y="{MARGIN_T + PLOT_H + 18}" text-anchor="end" fill="#6e7681" class="label">今天</text>

  {f'<polyline points="{ndq_line}" fill="none" stroke="#58a6ff" stroke-width="1.5" opacity="0.85"/>' if ndq_line else ''}
  {f'<polyline points="{gold_line}" fill="none" stroke="#f0a500" stroke-width="1.5" opacity="0.85"/>' if gold_line else ''}
  {f'<polyline points="{total_line}" fill="none" stroke="#d29922" stroke-width="2.5"/>' if total_line else ''}
  <text x="{W - MARGIN_R - 10}" y="{MARGIN_T + 18}" text-anchor="end" fill="{arrow_color}" font-size="22" font-weight="bold">{arrow}</text>

  <!-- 折线图图例 -->
  <g transform="translate({MARGIN_L + 8}, {MARGIN_T + 12})" class="label">
    <line x1="0" y1="0" x2="14" y2="0" stroke="#d29922" stroke-width="2.5"/>
    <text x="20" y="4" fill="#c9d1d9" font-weight="bold">Total</text>
    <line x1="80" y1="0" x2="94" y2="0" stroke="#58a6ff" stroke-width="1.5"/>
    <text x="100" y="4" fill="#c9d1d9">NDQ.AX</text>
    <line x1="170" y1="0" x2="184" y2="0" stroke="#f0a500" stroke-width="1.5"/>
    <text x="190" y="4" fill="#c9d1d9">Gold</text>
  </g>

  <!-- ===== 分隔线 ===== -->
  <line x1="{MARGIN_L}" y1="{LINE_H + 10}" x2="{W - MARGIN_R}" y2="{LINE_H + 10}"
        stroke="#21262d" stroke-width="1"/>

  <!-- ===== 下半：横向柱状图（vs N 基准 + 用户实盘）===== -->
  <text x="{MARGIN_L}" y="{LINE_H + 38}" fill="#c9d1d9" class="title">🏆 vs {len(benchmark_series)} 基准 · 截至今日累计涨幅 (60 天，sample size 较小不构成 alpha 证据)</text>

  {zero_line_svg}
  {chr(10).join(bar_svg)}
</svg>
"""


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
            return {"pushed": False, "reason": f"push failed: {push.stderr[:200]}",
                    "branch": branch}
        return {"pushed": True, "branch": branch, "mode": "main"}

    except subprocess.CalledProcessError as e:
        return {"pushed": False, "reason": f"git failure: {e.stderr[:200] if e.stderr else e}"}
    except Exception as e:
        return {"pushed": False, "reason": f"unexpected: {type(e).__name__}: {e}"}


def _is_trading_window(now: Optional[datetime] = None) -> bool:
    """是否在交易时段（按**北京时间**判断，不受服务器本地时区影响）。

    服务器可能跑在 UTC（容器或国外 VPS），datetime.now() 直接拿 hour
    会按 UTC 算，导致北京凌晨 4 点（UTC 20 点）被误判成"在 9-23 范围内"
    照常采样，写出折线图噪声。强制 .astimezone(+08:00) 解决。

    cron 表达式已经限制工作日 9/11/.../23 点 / 每 2h 触发；这里是手动
    `python -m jobs.pnl_snapshot` 调试时的二次保护。
    """
    from datetime import timezone, timedelta
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bj = now.astimezone(timezone(timedelta(hours=8)))
    if bj.weekday() >= 5:
        return False
    return 9 <= bj.hour <= 23


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
