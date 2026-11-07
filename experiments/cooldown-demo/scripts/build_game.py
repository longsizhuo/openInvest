"""生成「试玩：你 vs AI」的静态数据 data/game_data.json（行情 + 委员会每周决定）。

AI 一方 = 回测舰队在 memory/.backtest/<日>/<标的>.md 里留下的委员会决定：每个决定只喂当天之前的
行情，模型训练截止早于 2026 年（ADR-022），跟实盘用的是同一套委员会。每周取第一个有决定的交易日；
那一周没有决定就记 None，盲局只从连续记录完整的区间抽题。公开数据不含 CIO 原始理由。

跑（从仓库根，读生产数据目录）：
    INVEST_HOME=<生产 checkout> uv run python experiments/cooldown-demo/scripts/build_game.py
"""
from __future__ import annotations

import itertools
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ASSETS = [  # (页面 key, 显示名, yfinance 代码)
    ("gold", "黄金", "GC=F"),
    ("ndx", "纳指100", "QQQ"),
    ("csi300", "沪深300", "510300.SS"),
    ("tencent", "腾讯控股", "0700.HK"),
    ("sp500", "标普500", "^GSPC"),
    ("dow", "道琼斯工业 ETF", "DIA"),
    ("russell", "罗素2000 ETF", "IWM"),
    ("japan", "日本股市 ETF", "EWJ"),
    ("germany", "德国股市 ETF", "EWG"),
    ("emerging", "新兴市场 ETF", "EEM"),
    ("csi500", "中证500 ETF", "510500.SS"),
    ("star50", "科创50 ETF", "588000.SS"),
]
CHART_FROM = "2025-10-01"   # 图上第一周前留一个季度的走势当背景
GAME_FROM = "2026-01-01"
GAME_TO = "2026-09-30"     # 停在上个季度末：最近几周的决定离"现在"太近，像当下建议（细则 4.9）
WHY_MAX = 80
OUT = Path(__file__).resolve().parent.parent / "data" / "game_data.json"

_VERDICT_RE = re.compile(r"^\*\*Verdict\*\*:\s*([A-Z]+)", re.M)
_KV_RE = re.compile(r"^[A-Z_]{3,}:")
_CIO_RE = re.compile(r"^## CIO Memo[^\n]*\n(.*?)(?=^## |\Z)", re.S | re.M)


def parse_transcript(text: str) -> Tuple[Optional[str], str]:
    """委员会纪要 → (裁决, 理由)。置信度不要：比赛版对外不给置信度。理由取 CIO 备忘录的【结论】句，没有就取第一句。"""
    m = _VERDICT_RE.search(text)
    if not m:
        return None, ""
    verdict = m.group(1)
    memo = _CIO_RE.search(text)
    why = ""
    if memo:
        lines = [ln.strip() for ln in memo.group(1).splitlines() if ln.strip() and not ln.startswith("```")]
        # 文本模式的备忘录是 VERDICT:/CONFIDENCE:/... 键值块，没有散文理由——那就不给理由，别把键值行当理由
        prose = list(itertools.takewhile(lambda ln: not _KV_RE.match(ln), lines))
        hit = next((ln for ln in lines if ln.startswith("【结论】")), None) or (prose[0] if prose else "")
        why = re.sub(r"[*#`>]", "", re.sub(r"^【[^】]*】", "", hit)).strip()   # 【结论】【裁决结论】等标签不要
        if len(why) > WHY_MAX:
            why = why[:WHY_MAX].rstrip("，、；,; ") + "…"
    return verdict, why


def pick_turns(days: List[str], decisions: Dict[str, Tuple[str, str]], start: str) -> List[Dict[str, Any]]:
    """每个自然周一个决策点：该周第一个有决定的交易日，没有就用该周第一个交易日（v=None）。
    最后一个交易日只用来结算，不再出题。days 为升序交易日（含 start 之前的背景段）。"""
    weeks: Dict[Tuple[int, int], List[int]] = {}
    for i, d in enumerate(days[:-1]):
        if d >= start:
            weeks.setdefault(date.fromisoformat(d).isocalendar()[:2], []).append(i)
    turns = []
    for idx in weeks.values():
        i = next((j for j in idx if days[j] in decisions), idx[0])
        v, why = decisions.get(days[i], (None, ""))
        turns.append({"i": i, "v": v, "why": why})
    return turns


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", symbol)


def validate_asset(days: List[str], closes: List[float], turns: List[Dict[str, Any]]) -> None:
    """题库必须有真实、完整的行情与 AI 记录；不拿缺失记录冒充持有。"""
    if len(days) != len(closes) or days != sorted(set(days)):
        raise ValueError("行情日期必须唯一、有序，并与价格逐日对应")
    if not closes or any(not math.isfinite(c) or c <= 0 for c in closes):
        raise ValueError("行情价格必须为有限正数")
    longest = max((sum(1 for _ in g) for complete, g in itertools.groupby(turns, key=lambda t: bool(t["v"])) if complete), default=0)
    if longest < 12:
        raise ValueError("每个标的必须至少有连续 12 周完整的 AI 决策")
    indices = [t["i"] for t in turns]
    if indices != sorted(set(indices)) or indices[0] < 1 or indices[-1] >= len(days) - 1:
        raise ValueError("决策点必须有序，并保留开局背景和最终结算日")


def build(root: Path, *, cached: bool = False) -> Dict[str, Any]:
    if cached:
        from openinvest.db.market_store import MarketStore
        store = MarketStore()
        history = lambda code: store.get_history_df(code, days=10000)
    else:
        from openinvest.utils.exchange_fee import get_history_data
        history = lambda code: get_history_data(code, "2y")

    out = []
    for key, name, code in ASSETS:
        df = history(code)
        d = df.index.strftime("%Y-%m-%d")
        df = df[(d >= CHART_FROM) & (d <= GAME_TO)]
        days = list(df.index.strftime("%Y-%m-%d"))
        closes = [round(float(x), 4) for x in df["Close"]]
        decisions = {}
        for d in days:
            f = root / "memory" / ".backtest" / d / f"{_safe(code)}.md"
            if f.exists():
                v, why = parse_transcript(f.read_text(encoding="utf-8", errors="replace"))
                if v:
                    decisions[d] = (v, why)
        turns = pick_turns(days, decisions, GAME_FROM)
        missing = sum(t["v"] is None for t in turns)
        print(f"{code}: {len(days)} 天行情，{len(turns)} 周，其中 {missing} 周没有 AI 决定", file=sys.stderr)
        validate_asset(days, closes, turns)
        # 公开盲局只需动作；原始理由可能含标的、日期、绝对价位，不随题库导出。
        public_turns = [{"i": t["i"], "v": t["v"]} for t in turns]
        out.append({"key": key, "name": name, "code": code, "d": days, "c": closes, "turns": public_turns})
    return {"generated": date.today().isoformat(), "assets": out}


if __name__ == "__main__":
    import argparse
    from openinvest.paths import INVEST_ROOT

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true", help="只读本机行情缓存，不请求网络行情或调用模型")
    args = parser.parse_args()
    data = build(INVEST_ROOT, cached=args.cached)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)", file=sys.stderr)
