"""services/committee_charts —— 委员会详情页的图表/卡片 SVG 生成

纯函数：吃已经算好的 dict，出 HTML/SVG 字符串，零 IO。数据本身（path_profile /
regime_probability）来自 core/regime_probability.py，在调用方按需重新算
（零 LLM、零额外网络调用——纯 OHLC 历史统计），本模块只管渲染。

配色遵循 dataviz skill 的参考色板（scripts/validate_palette.js 验证过）：
- 裁决用 status palette（good/warning/serious/critical，语义状态色，不挪作系列色）
- 路径形状 4 分类用 categorical slot 1-4（blue/aqua/yellow/green，固定顺序）
- 30/60/90d 区间图用同一色相的 3 级 sequential 深浅（同一指标不同窗口，
  不是不同类别，不该用 categorical hue）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# 色板（dataviz skill references/palette.md，跑过 validate_palette.js 校验）
# ---------------------------------------------------------------------------

_STATUS = {
    # 裁决 → 语义状态色。BUY/ACCUMULATE 是"更积极"的两档，用同色不同深浅；
    # HOLD 中性；TRIM/SELL 是"更谨慎"的两档。
    "BUY": ("#0ca30c", "#1ecb1e"),
    "ACCUMULATE": ("#0ca30c", "#1ecb1e"),
    "HOLD": ("#52514e", "#8a8878"),
    "TRIM": ("#fab219", "#ffcf6b"),
    "SELL": ("#d03b3b", "#ff6b6b"),
}
_STATUS_DEFAULT = ("#52514e", "#8a8878")

# 路径形状 4 分类，固定顺序 = CVD 安全序（validate_palette.js 校验过）
_SHAPE_SLOTS = [
    ("pct_dip_then_up", "先跌后涨", "#2a78d6", "#3987e5"),
    ("pct_up_no_dip", "直接涨", "#1baf7a", "#199e70"),
    ("pct_pop_then_down", "冲高回落", "#eda100", "#c98500"),
    ("pct_down_no_pop", "一路收跌", "#008300", "#008300"),
]

# 30/60/90d 同指标不同窗口 → sequential 蓝，深浅按窗口远近递进（ordinal 阶梯）
_WINDOW_SEQ = {
    "30d": ("#86b6ef", "#5598e7"),
    "60d": ("#5598e7", "#2a78d6"),
    "90d": ("#2a78d6", "#184f95"),
}

_TEXT_MUTED_LIGHT, _TEXT_MUTED_DARK = "#75736a", "#9c9a8d"
_GRID_LIGHT, _GRID_DARK = "#e4e2db", "#3a3937"


def _esc(v: Any) -> str:
    import html
    return html.escape(str(v))


def render_verdict_tile(verdict: Dict[str, Any]) -> str:
    """裁决 + 置信度的大字号状态卡片。verdict 是状态而非分类系列——用
    status palette（语义色，never 挪作系列色），不是 categorical 色板。
    """
    v = str(verdict.get("verdict") or "?").upper()
    light, dark = _STATUS.get(v, _STATUS_DEFAULT)
    confidence = verdict.get("confidence")
    conf_pct = f"{confidence * 100:.0f}%" if isinstance(confidence, (int, float)) else "—"
    dominant = _esc(verdict.get("dominant_view") or "—")
    alloc = verdict.get("alloc_cny")
    alloc_txt = f"¥{alloc:+,.0f}" if isinstance(alloc, (int, float)) else "—"

    return f"""
<div class="verdict-tile" style="--v-light:{light}; --v-dark:{dark};">
  <div class="verdict-badge">{_esc(v)}</div>
  <div class="verdict-meta">
    <span>置信度 <b>{_esc(conf_pct)}</b></span>
    <span>主导 <b>{dominant}</b></span>
    <span>建议 <b>{_esc(alloc_txt)}</b></span>
  </div>
</div>
""".strip()


def render_path_shape_chart(shape: Optional[Dict[str, Any]]) -> str:
    """90d 路径形状分布——4 类互斥结果的横向条形图。

    这是回答"接下来大概率怎么走"最直接的一张图：先跌后涨/直接涨/冲高回落/
    一路收跌四选一，条形长度 = 历史同 regime 下发生该形状的概率。
    """
    if not shape:
        return ""
    n = shape.get("n")
    eff_n = shape.get("effective_n")
    window = shape.get("window", "90d")

    bar_h, gap, label_w, track_w = 22, 8, 88, 360
    rows = []
    y = 0
    for key, label, light, dark in _SHAPE_SLOTS:
        pct = float(shape.get(key) or 0.0) * 100
        w = max(2, track_w * pct / 100)
        rows.append(f"""
    <g transform="translate(0,{y})">
      <text x="{label_w - 10}" y="{bar_h / 2 + 4}" text-anchor="end" class="chart-label">{_esc(label)}</text>
      <rect x="{label_w}" y="0" width="{track_w}" height="{bar_h}" rx="4" class="chart-track"/>
      <rect x="{label_w}" y="0" width="{w:.1f}" height="{bar_h}" rx="4"
            class="chart-fill" style="--s-light:{light}; --s-dark:{dark};">
        <title>{_esc(label)}: {pct:.0f}%</title>
      </rect>
      <text x="{label_w + track_w + 8}" y="{bar_h / 2 + 4}" class="chart-value">{pct:.0f}%</text>
    </g>""")
        y += bar_h + gap

    total_w = label_w + track_w + 48
    total_h = y - gap
    return f"""
<div class="chart-card">
  <div class="chart-title">90 天路径形状分布 <span class="chart-sub">(n={n}, 独立样本≈{eff_n})</span></div>
  <svg viewBox="0 0 {total_w} {total_h}" width="100%" height="{total_h}" role="img"
       aria-label="90天路径形状分布：先跌后涨/直接涨/冲高回落/一路收跌四类概率">
    {''.join(rows)}
  </svg>
  <div class="chart-footnote">"显著"= 途中波幅 ≥1×当日 ATR；窗口 {_esc(window)}</div>
</div>
""".strip()


def render_forward_return_chart(windows: Optional[Dict[str, Dict[str, Any]]]) -> str:
    """30/60/90d 历史 forward return 分布——p10-p90 区间 + 中位数刻度 + 悲观分位标记。

    这是 CIO 判断"值不值得现在动"时实际参照的数据：不是单点预测，是一段
    历史同 regime 下的收益分布带。
    """
    if not windows:
        return ""
    order = [w for w in ("30d", "60d", "90d") if w in windows]
    if not order:
        return ""

    all_vals = []
    for w in order:
        st = windows[w]
        all_vals += [st.get("p10_pct", 0), st.get("p90_pct", 0), st.get("downside_pct", 0), 0]
    lo, hi = min(all_vals), max(all_vals)
    pad = max((hi - lo) * 0.12, 1.0)
    lo, hi = lo - pad, hi + pad
    axis_w = 380

    def _x(pct: float) -> float:
        return (pct - lo) / (hi - lo) * axis_w

    row_h, gap, label_w = 34, 10, 46
    rows = []
    y = 0
    zero_x = _x(0)
    for w in order:
        st = windows[w]
        p10, p90 = st.get("p10_pct", 0.0), st.get("p90_pct", 0.0)
        median = st.get("median_pct", 0.0)
        downside = st.get("downside_pct")
        light, dark = _WINDOW_SEQ.get(w, _WINDOW_SEQ["30d"])
        x1, x2 = _x(p10), _x(p90)
        med_x = _x(median)
        low_conf = st.get("low_confidence")
        rows.append(f"""
    <g transform="translate(0,{y})">
      <text x="{label_w - 10}" y="{row_h / 2 + 4}" text-anchor="end" class="chart-label">{_esc(w)}{' ⚠' if low_conf else ''}</text>
      <line x1="{label_w + x1:.1f}" y1="{row_h / 2}" x2="{label_w + x2:.1f}" y2="{row_h / 2}"
            class="range-band" style="--band-light:{light}; --band-dark:{dark};">
        <title>{_esc(w)}: 中位 {median:+.1f}%，10-90 分位 [{p10:+.1f}%, {p90:+.1f}%]</title>
      </line>
      <circle cx="{label_w + med_x:.1f}" cy="{row_h / 2}" r="5" class="range-median"/>
      <text x="{label_w + med_x:.1f}" y="{row_h / 2 - 10}" text-anchor="middle" class="chart-value-sm">{median:+.1f}%</text>
      {"" if downside is None else f'<line x1="{label_w + _x(downside):.1f}" y1="{row_h/2-8}" x2="{label_w + _x(downside):.1f}" y2="{row_h/2+8}" class="range-downside"><title>悲观情形(20分位): {downside:+.1f}%</title></line>'}
    </g>""")
        y += row_h + gap

    total_h = y - gap
    total_w = label_w + axis_w + 20
    return f"""
<div class="chart-card">
  <div class="chart-title">历史 forward return 分布（同 regime 条件下）</div>
  <svg viewBox="0 0 {total_w} {total_h}" width="100%" height="{total_h}" role="img"
       aria-label="30/60/90天历史收益分布区间图，含中位数与悲观分位">
    <line x1="{label_w + zero_x:.1f}" y1="0" x2="{label_w + zero_x:.1f}" y2="{total_h}" class="range-zero"/>
    {''.join(rows)}
  </svg>
  <div class="chart-footnote">灰点 = 中位数 · 红色竖线 = 悲观情形(20分位，TRIM 买回点参照) · 带宽 = 10-90 分位 · ⚠ = 独立样本 &lt;10，仅供参考</div>
</div>
""".strip()


CHART_CSS = """
.verdict-tile { display:flex; align-items:center; gap:14px; padding:14px 18px; margin:6px 0 18px;
  background: color-mix(in srgb, var(--v-light) 10%, transparent); border-left:4px solid var(--v-light); border-radius:8px; }
.verdict-badge { font-size:22px; font-weight:700; color:var(--v-light); letter-spacing:0.5px; }
.verdict-meta { display:flex; gap:16px; flex-wrap:wrap; font-size:13.5px; color:#52514e; }
.verdict-meta b { color:#1a2533; }
.chart-card { margin:20px 0; padding:16px 18px; background:#fcfcfb; border-radius:10px; border:1px solid #e4e2db; }
.chart-title { font-size:14px; font-weight:600; color:#1a2533; margin-bottom:10px; }
.chart-sub { font-weight:400; color:#75736a; font-size:12.5px; }
.chart-label { font-size:12px; fill:#52514e; font-family: -apple-system, sans-serif; }
.chart-value { font-size:12.5px; fill:#1a2533; font-weight:600; dominant-baseline:middle; }
.chart-value-sm { font-size:10.5px; fill:#1a2533; font-weight:600; }
.chart-track { fill:#e4e2db; }
.chart-fill rect, .chart-fill { fill: var(--s-light); }
.chart-footnote { margin-top:10px; font-size:11.5px; color:#75736a; }
.range-band { stroke: var(--band-light); stroke-width:6; stroke-linecap:round; }
.range-median { fill:#1a2533; stroke:#fcfcfb; stroke-width:2; }
.range-downside { stroke:#d03b3b; stroke-width:2; }
.range-zero { stroke:#c9c7bc; stroke-width:1.5; stroke-dasharray:3 3; }
@media (prefers-color-scheme: dark) {
  .verdict-tile { background: color-mix(in srgb, var(--v-dark) 16%, transparent); border-left-color:var(--v-dark); }
  .verdict-badge { color:var(--v-dark); }
  .verdict-meta { color:#c3c2b7; }
  .verdict-meta b { color:#ffffff; }
  .chart-card { background:#232320; border-color:#3a3937; }
  .chart-title { color:#ffffff; }
  .chart-sub, .chart-footnote { color:#9c9a8d; }
  .chart-label { fill:#c3c2b7; }
  .chart-value, .chart-value-sm { fill:#ffffff; }
  .chart-track { fill:#3a3937; }
  .chart-fill rect, .chart-fill { fill: var(--s-dark); }
  .range-band { stroke: var(--band-dark); }
  .range-median { fill:#ffffff; stroke:#232320; }
  .range-zero { stroke:#4a4946; }
}
"""

__all__ = [
    "render_verdict_tile",
    "render_path_shape_chart",
    "render_forward_return_chart",
    "CHART_CSS",
]
