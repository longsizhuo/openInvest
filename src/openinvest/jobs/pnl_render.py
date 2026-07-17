"""PnL 图渲染 + outperform 事件纯核（域绑定纯模块，ADR-026）

从 jobs/pnl_snapshot.py 拆出的纯计算：SVG 几何/渲染、基准截面涨幅、
交易时段判定（now 必传）、跑赢/跑输事件计算。全部函数吃传入的数据
（history / benchmark_series / snap / now），零 IO——文件读写 / 基准刷新 /
git push / 时钟全留在 pnl_snapshot.py（IO shell，那边保留同名薄包装）。

monkeypatch 注意：pnl_snapshot 的同名包装在自己命名空间解析
_ensure_benchmarks_fresh / get_all_series / datetime.now——patch IO 打那边，
patch 渲染逻辑打本模块。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from openinvest.calc.series import BenchmarkSeries

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


def _pct_label_pos(
    pct: float, bar_x: float, bar_w: float, is_user: bool,
    bar_axis_left: float, bar_axis_right: float = 720,
) -> Tuple[float, str, Optional[str]]:
    """柱状图 % 标签的 (x, text-anchor, fill)。

    默认贴在条端外侧（正条右、负条左）。满宽条外侧可能越界：
    - 负向满宽：外侧向左压住左侧基准名 label（openInvest#92）
    - 正向满宽：外侧向右溢出画布右边距
    两种情况都翻到条内，并换对比色保证可读。
    fill=None 表示沿用条本身的颜色。
    """
    LABEL_W = 56  # ≈ "-100.00%" @ 11px 等宽字
    _inside_fill = "#0d1117" if is_user else "#f0f6fc"
    if pct >= 0:
        outside_x = bar_x + bar_w + 6
        if outside_x + LABEL_W <= bar_axis_right + 80:  # 80 = right margin
            return outside_x, "start", None
        # 正向满宽 → 翻到条内左端
        return bar_x + bar_w - 6, "end", _inside_fill
    if bar_x - 6 - LABEL_W >= bar_axis_left:
        return bar_x - 6, "end", None
    return bar_x + 6, "start", _inside_fill


def render_svg(
    history: List[Dict[str, Any]],
    benchmark_series: List[BenchmarkSeries],
) -> str:
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
        pct_x, pct_anchor, pct_fill = _pct_label_pos(
            pct, bar_x, bar_w, is_user, BAR_AXIS_LEFT, BAR_AXIS_RIGHT
        )
        bar_svg.append(
            f'<text x="{pct_x:.1f}" y="{y + BAR_ROW_H / 2 + 4:.1f}" '
            f'text-anchor="{pct_anchor}" fill="{pct_fill or color}" class="label" '
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

    # SVG 总高度（含底部口径脚注一行，issue #179 P1-A③）
    H_TOTAL = bar_y_end + BAR_BOTTOM_PAD + 18

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
    <text x="100" y="4" fill="#c9d1d9">资产 A</text>
    <line x1="170" y1="0" x2="184" y2="0" stroke="#f0a500" stroke-width="1.5"/>
    <text x="190" y="4" fill="#c9d1d9">资产 B</text>
  </g>

  <!-- ===== 分隔线 ===== -->
  <line x1="{MARGIN_L}" y1="{LINE_H + 10}" x2="{W - MARGIN_R}" y2="{LINE_H + 10}"
        stroke="#21262d" stroke-width="1"/>

  <!-- ===== 下半：横向柱状图（vs N 基准 + 用户实盘）===== -->
  <text x="{MARGIN_L}" y="{LINE_H + 38}" fill="#c9d1d9" class="title">🏆 vs {len(benchmark_series)} 基准 · 截至今日累计涨幅 ({WINDOW_DAYS} 天，sample size 较小不构成 alpha 证据)</text>

  {zero_line_svg}
  {chr(10).join(bar_svg)}

  <!-- 口径脚注：两侧统计口径不同，不可直接比大小（issue #179 P1-A③）-->
  <text x="{MARGIN_L}" y="{bar_y_end + BAR_BOTTOM_PAD + 8}" fill="#6e7681" class="label">口径：★ 实盘 = 现市值/累计成本 −1（分批建仓，含加仓时点效应）；基准 = 窗口首日一次性买入。口径不同，只看方向不比大小。</text>
</svg>
"""


def _is_trading_window(now: datetime) -> bool:
    """是否在交易时段（按**北京时间**判断，不受服务器本地时区影响）。

    服务器可能跑在 UTC（容器或国外 VPS），datetime.now() 直接拿 hour
    会按 UTC 算，导致北京凌晨 4 点（UTC 20 点）被误判成"在 9-23 范围内"
    照常采样，写出折线图噪声。强制 .astimezone(+08:00) 解决。

    cron 表达式已经限制工作日 9/11/.../23 点 / 每 2h 触发；这里是手动
    `python -m jobs.pnl_snapshot` 调试时的二次保护。纯核：now **必传**
    （时间是输入不是环境）——IO shell 的同名包装负责补当前时钟。
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    bj = now.astimezone(timezone(timedelta(hours=8)))
    if bj.weekday() >= 5:
        return False
    return 9 <= bj.hour <= 23


def _outperform_events(
    snap: Snapshot,
    history: List[Dict[str, Any]],
    all_series: List[BenchmarkSeries],
) -> List[Dict[str, Any]]:
    """对比所有基准的当前累计涨幅 → 找出"openInvest 跑赢 X 多少"的事件

    PM-3 增长杠杆：每次 pnl_snapshot 都生成事件化文本，让用户拥有"可分享的瞬间"
    （"我跑赢了余额宝 +2.3%"截图发朋友圈），而不是只有一张静态图。事件落盘到
    docs/outperform_events.jsonl 给后续 daily digest / web GUI 引用。

    返回 List[{"benchmark": str, "diff_pct": float, "label": str, "user_pct": float,
              "bench_pct": float, "ts": ISO}]，按 diff_pct 倒序。**diff_pct 必须 > 0**
    才算 outperform 事件 —— 落后基准时不生成"事件"（避免每次都报负面）。
    """
    if snap.total_pnl_pct is None:
        return []
    user_pct = float(snap.total_pnl_pct)

    # 用 history 的第一条 ts 作为对比起点（与 SVG 同源）
    if not history:
        return []
    start_date = history[0]["ts"][:10]

    # 金融视角红线：原版 `if diff <= 0: continue` 是 survivorship bias，对外
    # 展示只有 winning streak 构成误导性宣传。新版同时记 winning + losing 两类
    # 事件，label 主语用"作者账户"而非"openInvest"（工具本身没持仓，主语替换
    # 会被误读为工具能力背书）。
    events: List[Dict[str, Any]] = []
    for series in all_series:
        bench_pct = _latest_pct(series, start_date)
        if bench_pct is None:
            continue
        diff = user_pct - bench_pct
        win = diff > 0
        events.append({
            "ts": snap.ts,
            "benchmark": series.key,
            "user_pct": round(user_pct, 4),
            "bench_pct": round(bench_pct, 4),
            "diff_pct": round(diff, 4),
            "is_outperform": win,
            "label": (
                f"作者账户过去 {len(history)} 个数据点 "
                f"{'跑赢' if win else '跑输'}{series.key} {diff:+.2f}%"
            ),
        })
    # 按 |diff| 排序（绝对幅度大的优先展示），不再"只挑赢的"
    events.sort(key=lambda e: abs(e["diff_pct"]), reverse=True)
    return events



__all__ = [
    "W", "LINE_H", "BAR_ROW_H", "BAR_TOP_PAD", "BAR_BOTTOM_PAD",
    "MARGIN_L", "MARGIN_R", "MARGIN_T", "MARGIN_B", "PLOT_W", "PLOT_H",
    "WINDOW_DAYS",
    "Snapshot",
    "_project_y",
    "_series_polyline",
    "_latest_pct",
    "_pct_label_pos",
    "render_svg",
    "_is_trading_window",
    "_outperform_events",
]
