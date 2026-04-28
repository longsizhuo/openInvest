"""PnL 快照 + SVG 折线图生成

每小时跑一次。流程：
1. 读 portfolio.md（cash + ndq + gold）
2. 拉当前价（NDQ.AX / AUDCNY / 黄金现货 GC=F + USDCNY）
3. 算各类资产的浮盈百分比（**只算 %，不存绝对金额到 git**）
4. append 到 memory/.state/pnl_history.jsonl（git ignore）
5. 渲染 docs/pnl_chart.svg（入 git，但只含百分比线段，无明文数字）

隐私设计：
- 原始 jsonl 含 cash 等绝对值 → gitignore，永不入库
- SVG 只含百分比线段，且 axis 上不写任何数字 / 日期
- 看图能看出涨跌趋势 + 哪个资产贡献多，但读不出"今天浮盈多少元 / 资产规模多大"

触发方式：
- jobs/pnl_snapshot.yml 自动每小时跑
- 或手动: python -m jobs.pnl_snapshot
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

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


def render_svg(history: List[Dict[str, Any]]) -> str:
    """渲染折线图。**故意不写任何数字标签**，只显示线条、0% 基线、方向箭头。"""
    if not history:
        # 空状态：一句话占位
        return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-label="PnL trend chart (no data yet)">
  <rect width="{W}" height="{H}" fill="#0d1117"/>
  <text x="{W//2}" y="{H//2}" text-anchor="middle" fill="#8b949e" font-family="ui-monospace, monospace" font-size="14">
    [PnL chart — 数据采集中，请等待 jobs/pnl_snapshot 跑几次后查看]
  </text>
</svg>
"""

    # 算 y 轴范围（pad 10% 让线不顶到边缘）
    all_values: List[float] = []
    for entry in history:
        for k in ("total_pnl_pct", "ndq_pnl_pct", "gold_pnl_pct"):
            v = entry.get(k)
            if v is not None:
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

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" aria-label="PnL trend chart (privacy-preserving, no absolute numbers)">
  <style>
    .label {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }}
  </style>
  <!-- 背景 -->
  <rect width="{W}" height="{H}" fill="#0d1117"/>
  <!-- 0% 基线（虚线灰）-->
  <line x1="{MARGIN_L}" y1="{zero_y:.1f}" x2="{W - MARGIN_R}" y2="{zero_y:.1f}"
        stroke="#30363d" stroke-width="1" stroke-dasharray="4 4"/>
  <text x="{MARGIN_L - 6}" y="{zero_y + 4:.1f}" text-anchor="end" fill="#6e7681" class="label">0%</text>
  <!-- y 轴方向标识（不带数字）-->
  <text x="{MARGIN_L - 6}" y="{MARGIN_T + 10}" text-anchor="end" fill="#3fb950" class="label">+</text>
  <text x="{MARGIN_L - 6}" y="{H - MARGIN_B - 2}" text-anchor="end" fill="#f85149" class="label">−</text>
  <!-- x 轴标签：相对时间，无具体日期 -->
  <text x="{MARGIN_L}" y="{H - 8}" fill="#6e7681" class="label">30 天前</text>
  <text x="{W - MARGIN_R}" y="{H - 8}" text-anchor="end" fill="#6e7681" class="label">今天</text>

  <!-- 三条 series 线 -->
  {f'<polyline points="{ndq_line}" fill="none" stroke="#58a6ff" stroke-width="1.5" opacity="0.85"/>' if ndq_line else ''}
  {f'<polyline points="{gold_line}" fill="none" stroke="#f0a500" stroke-width="1.5" opacity="0.85"/>' if gold_line else ''}
  {f'<polyline points="{total_line}" fill="none" stroke="#d29922" stroke-width="2.5"/>' if total_line else ''}

  <!-- 当前趋势方向箭头（不带数字）-->
  <text x="{W - MARGIN_R - 10}" y="{MARGIN_T + 18}" text-anchor="end" fill="{arrow_color}" font-size="22" font-weight="bold">{arrow}</text>

  <!-- 图例 -->
  <g transform="translate({MARGIN_L + 8}, {MARGIN_T + 12})" class="label">
    <line x1="0" y1="0" x2="14" y2="0" stroke="#d29922" stroke-width="2.5"/>
    <text x="20" y="4" fill="#c9d1d9">Total</text>
    <line x1="70" y1="0" x2="84" y2="0" stroke="#58a6ff" stroke-width="1.5"/>
    <text x="90" y="4" fill="#c9d1d9">NDQ.AX</text>
    <line x1="155" y1="0" x2="169" y2="0" stroke="#f0a500" stroke-width="1.5"/>
    <text x="175" y="4" fill="#c9d1d9">Gold</text>
  </g>
</svg>
"""


def run() -> Dict[str, Any]:
    """job entry：算快照 + 写历史 + 渲染 SVG"""
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

    return {
        "status": "ok",
        "ts": snap.ts,
        "history_points": len(history),
        "svg_path": str(SVG_PATH),
        # **故意不在 return 里暴露百分比数字**，避免 scheduler 日志泄露
        "trend": "up" if snap.total_pnl_pct > 0 else (
            "down" if snap.total_pnl_pct < 0 else "flat"
        ),
    }


if __name__ == "__main__":
    print(run())
