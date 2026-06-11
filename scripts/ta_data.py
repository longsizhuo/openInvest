"""test_ta Phase A 数据层 — point-in-time 数据包（零前视）+ 外部数据缓存。

三个分析师的数据标签（Phase 0 spike 结论，2026-06-11）：
- 情绪：真实（^VIX 本地全历史滚动分位 + 资产 ATR 突变比，生产口径）
- 基本面-黄金：真实（CFTC COT 金期货持仓周报，as_of+3 日发布滞后对齐）
- 基本面-NDX：降级（免费历史 PE 不存在 → OHLC 估值代理 + ^TNX 利率）
- 新闻：真实·降级（GDELT 2024-26 历史头条，仅标题无正文，1req/6s 节流缓存）

所有切片严格 `df[df.index <= asof]`；COT 只用 as_of ≤ asof-3天 的报告。
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.market_store import MarketStore  # noqa: E402

CACHE = ROOT / "memory" / ".ta_experiment"
CACHE.mkdir(parents=True, exist_ok=True)

ASSET_LABEL = {"GC=F": "Gold futures (GC=F)", "NDQ.AX": "Nasdaq-100 ETF (NDQ.AX)"}


def load_dates() -> Tuple[List[str], List[str]]:
    d = json.loads((CACHE / "decision_dates.json").read_text())
    return d["dates"], d["assets"]


# ---------------------------------------------------------------------------
# 行情序列（本地 DB，point-in-time 切片）
# ---------------------------------------------------------------------------
_HIST: Dict[str, pd.DataFrame] = {}


def hist(symbol: str) -> pd.DataFrame:
    if symbol not in _HIST:
        df = MarketStore().get_history_df(symbol, days=20000)
        df = df[~df.index.duplicated(keep="last")]
        _HIST[symbol] = df
    return _HIST[symbol]


def closes_until(symbol: str, asof: str) -> pd.Series:
    df = hist(symbol)
    if df is None or df.empty or "Close" not in df:
        raise RuntimeError(f"行情库无 {symbol} 数据（worktree 是否缺 db/market_data.db？）")
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return s[s.index <= pd.Timestamp(asof)].dropna()


def _atr_pct_series(symbol: str, asof: str) -> Optional[pd.Series]:
    """生产口径 Wilder 14d ATR%（复用 utils.market_metrics 的算法语义）"""
    df = hist(symbol)
    df = df[df.index <= pd.Timestamp(asof)]
    if len(df) < 30:
        return None
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if "High" in df.columns and df["High"].notna().sum() > len(df) * 0.5:
        high, low = df["High"], df["Low"]
        prev = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - prev).abs(), (low - prev).abs()],
                       axis=1).max(axis=1)
    else:
        tr = close.diff().abs()
    return (tr.ewm(alpha=1 / 14, adjust=False).mean() / close * 100).dropna()


# ---------------------------------------------------------------------------
# 情绪包（真实：VIX 分位 + ATR 突变比）
# ---------------------------------------------------------------------------
def pack_sentiment(symbol: str, asof: str) -> Tuple[str, str]:
    """返回 (数据包文本, 确定性基线③ stance)。

    基线③（预注册）：VIX 2y 分位 ≥0.85→bearish（naive 恐慌读法）、
    ≤0.35→bullish、其余 neutral。LLM 必须打赢这个机械映射才算增量。
    """
    vix = closes_until("^VIX", asof)
    win = vix.tail(504)
    last = float(win.iloc[-1])
    pct = float((win <= last).mean())
    vix_4w = float(win.iloc[-1] / win.iloc[-20] - 1) if len(win) > 20 else 0.0
    atr = _atr_pct_series(symbol, asof)
    spike = None
    if atr is not None and len(atr) > 260:
        spike = float(atr.iloc[-1] / atr.rolling(252, min_periods=120).median().iloc[-1])
    px = closes_until(symbol, asof)
    r30 = float(px.iloc[-1] / px.iloc[-21] - 1) if len(px) > 21 else 0.0

    lines = [
        f"## Volatility / fear-greed dashboard (as of {asof}, all point-in-time)",
        f"- VIX last: {last:.1f}  | 2-year percentile: {pct*100:.0f}%  "
        f"(>=85% = extreme fear zone, <=35% = greed zone)",
        f"- VIX 4-week change: {vix_4w:+.1%}",
    ]
    if spike is not None:
        lines.append(
            f"- {symbol} ATR volatility spike ratio (today ATR% / 1y median): {spike:.2f}")
    lines.append(f"- {symbol} trailing 30-day return: {r30:+.1%}")
    text = "\n".join(lines) + "\n"

    det = "bearish" if pct >= 0.85 else ("bullish" if pct <= 0.35 else "neutral")
    return text, det


# ---------------------------------------------------------------------------
# COT（真实：CFTC 金期货持仓，+3 日发布滞后）
# ---------------------------------------------------------------------------
_COT_CACHE = CACHE / "cot_gold.csv"


def ensure_cot() -> pd.DataFrame:
    if _COT_CACHE.exists():
        return pd.read_csv(_COT_CACHE, parse_dates=["as_of"])
    frames = []
    for year in (2024, 2025, 2026):
        url = f"https://www.cftc.gov/files/dea/history/deacot{year}.zip"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)"})
            raw = urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:  # noqa: BLE001
            print(f"COT {year} 下载失败: {e}")
            continue
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = z.namelist()[0]
            df = pd.read_csv(z.open(name), low_memory=False)
        df = df[df["Market and Exchange Names"].str.contains("GOLD - COMMODITY EXCHANGE", na=False)]
        frames.append(df)
    cols = {
        "As of Date in Form YYYY-MM-DD": "as_of",
        "Open Interest (All)": "open_interest",
        "Noncommercial Positions-Long (All)": "spec_long",
        "Noncommercial Positions-Short (All)": "spec_short",
    }
    out = pd.concat(frames)[list(cols)].rename(columns=cols)
    out["as_of"] = pd.to_datetime(out["as_of"])
    out["net_spec"] = out["spec_long"] - out["spec_short"]
    # 同一 as_of 命中多条 GOLD 市场（主力 + micro 等）→ 留 OI 最大的主力合约行
    out = (out.sort_values(["as_of", "open_interest"])
              .groupby("as_of", as_index=False).last()
              .sort_values("as_of").reset_index(drop=True))
    out.to_csv(_COT_CACHE, index=False)
    return out


def pack_fundamental(symbol: str, asof: str) -> Tuple[str, str]:
    """基本面包 + 确定性基线③。

    GC=F（真实）：COT 净投机持仓 8 周表；基线③ = 4 周净持仓变化符号。
    NDQ.AX（降级）：OHLC 估值代理 + ^TNX；基线③ = 2y 分位 ≥0.8→bearish ≤0.2→bullish。
    """
    asof_ts = pd.Timestamp(asof)
    if symbol == "GC=F":
        cot = ensure_cot()
        avail = cot[cot["as_of"] <= asof_ts - timedelta(days=3)].tail(8)
        lines = [f"  {r.as_of.date()}  net_spec={r.net_spec:+,}  OI={r.open_interest:,}"
                 for r in avail.itertuples()]
        px = closes_until(symbol, asof)
        q2y = float((px.tail(504) <= px.iloc[-1]).mean())
        text = (
            f"## Gold positioning fundamentals (as of {asof}, point-in-time; "
            f"COT reports lag ~3 days after their as-of date)\n"
            f"### CFTC COT — gold futures, non-commercial (speculative) positioning, last 8 weekly reports\n"
            + "\n".join(lines)
            + f"\n- Gold price 2-year percentile: {q2y*100:.0f}%\n"
            "Note: rising net speculative longs = momentum/crowding; extreme readings can be contrarian.\n"
        )
        det = "neutral"
        if len(avail) >= 5:
            delta = float(avail["net_spec"].iloc[-1] - avail["net_spec"].iloc[-5])
            det = "bullish" if delta > 0 else ("bearish" if delta < 0 else "neutral")
        return text, det

    # NDQ.AX —— 降级口径（无免费历史 PE）
    px = closes_until(symbol, asof)
    q2y = float((px.tail(504) <= px.iloc[-1]).mean())
    ma120 = float(px.rolling(120).mean().iloc[-1])
    ma250 = float(px.rolling(250).mean().iloc[-1]) if len(px) >= 250 else None
    dev120 = px.iloc[-1] / ma120 - 1
    ath = float(px.cummax().iloc[-1])
    dd = px.iloc[-1] / ath - 1
    tnx = closes_until("^TNX", asof)
    tnx_line = ""
    if len(tnx) > 63:
        tnx_line = (f"- US 10y yield (^TNX): {float(tnx.iloc[-1]):.2f}%  "
                    f"(3-month change: {float(tnx.iloc[-1]-tnx.iloc[-63]):+.2f}pp)\n")
    text = (
        f"## Valuation proxies for {ASSET_LABEL[symbol]} (as of {asof}, point-in-time)\n"
        f"DATA LIMITATION: historical index P/E is not available; the proxies below are\n"
        f"price-based valuation gauges plus the rate environment.\n"
        f"- Price 2-year percentile: {q2y*100:.0f}%\n"
        f"- Deviation from 120-day MA: {dev120:+.1%}"
        + (f"; from 250-day MA: {px.iloc[-1]/ma250-1:+.1%}\n" if ma250 else "\n")
        + f"- Drawdown from all-time high: {dd:+.1%}\n"
        + tnx_line
    )
    det = "bearish" if q2y >= 0.8 else ("bullish" if q2y <= 0.2 else "neutral")
    return text, det


# ---------------------------------------------------------------------------
# GDELT 新闻（真实·降级：仅标题；节流缓存）
# ---------------------------------------------------------------------------
_NEWS_CACHE = CACHE / "news_cache.jsonl"
GDELT_QUERIES = {
    "NDQ.AX": '(nasdaq OR "tech stocks" OR "us stocks")',
    "GC=F": '("gold price" OR "gold demand" OR "central bank gold" OR bullion)',
}


def _news_cache() -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    if _NEWS_CACHE.exists():
        for line in _NEWS_CACHE.open():
            r = json.loads(line)
            out[r["key"]] = r["articles"]
    return out


_COMBINED_QUERY = ('(nasdaq OR "tech stocks" OR "us stocks" OR "gold price" '
                   'OR bullion OR "central bank gold" OR "gold demand")')
_GOLD_KW = ("gold", "bullion", "xau", "precious metal")
_NDQ_KW = ("nasdaq", "tech", "stock", "equit", "s&p", "dow", "fed", "rate",
           "inflation", "treasury")


def fetch_gdelt_all(throttle_s: float = 18.0) -> None:
    """每个决策日一次合并请求（两资产共用），客户端按关键词分流到两个缓存 key。

    为什么合并（2026-06-11 改）：本环境 IP 被 GDELT 重度限流（实测 ~75% 429），
    每日两请求 → 一请求，墙钟减半；客户端关键词过滤的相关性不输原小查询
    （GDELT hybridrel 排序本来就噪）。失败日不写缓存（防投毒），断点续抓。
    """
    dates, assets = load_dates()
    cache = _news_cache()
    todo = [d for d in dates if any(f"{d}|{a}" not in cache for a in assets)]
    print(f"GDELT 待抓 {len(todo)} 个日期（已有 key {len(cache)}）", flush=True)
    with _NEWS_CACHE.open("a") as fh:
        for i, d in enumerate(todo):
            start = (pd.Timestamp(d) - timedelta(days=7)).strftime("%Y%m%d%H%M%S")
            end = pd.Timestamp(d).strftime("%Y%m%d") + "000000"
            q = urllib.request.quote(_COMBINED_QUERY)
            url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
                   f"&mode=artlist&maxrecords=40&format=json&sort=hybridrel"
                   f"&startdatetime={start}&enddatetime={end}")
            # 单次尝试：快速重试会触发更长的 IP 级锁定（实测）
            arts: Optional[List[dict]] = None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "openInvest-research/0.1"})
                raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
                data = json.loads(raw)   # 429 返回纯文本 → 这里抛
                arts = [{"title": x.get("title", ""), "domain": x.get("domain", ""),
                         "seendate": x.get("seendate", "")}
                        for x in (data.get("articles") or [])]
            except urllib.error.HTTPError as e:
                print(f"  miss {d} (HTTP {e.code})", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  miss {d} ({type(e).__name__}: {str(e)[:60]})", flush=True)
            if arts is None:
                time.sleep(throttle_s + 40)   # miss 后额外冷却，给限流器降温
                continue   # 失败不写缓存（写空=缓存投毒，续跑永久跳过）
            # 客户端关键词分流（标题小写匹配，两边不沾的丢弃；每资产截 15 条）
            buckets: Dict[str, List[dict]] = {a: [] for a in assets}
            for x in arts:
                t = (x["title"] or "").lower()
                if "GC=F" in buckets and any(k in t for k in _GOLD_KW):
                    if len(buckets["GC=F"]) < 15:
                        buckets["GC=F"].append(x)
                elif "NDQ.AX" in buckets and any(k in t for k in _NDQ_KW):
                    if len(buckets["NDQ.AX"]) < 15:
                        buckets["NDQ.AX"].append(x)
            for a in assets:
                if f"{d}|{a}" not in cache:
                    fh.write(json.dumps({"key": f"{d}|{a}", "articles": buckets[a]},
                                        ensure_ascii=False) + "\n")
            fh.flush()
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(todo)}", flush=True)
            time.sleep(throttle_s)
    print("GDELT 抓取完成", flush=True)


def pack_news(symbol: str, asof: str) -> Tuple[str, Optional[str]]:
    """新闻包（真实头条）。基线③ = None（标题无确定性 stance 可推导，
    预注册改用方向基率作为该分析师的对照线）。"""
    arts = _news_cache().get(f"{asof}|{symbol}", [])
    if not arts:
        text = (f"## News headlines for {ASSET_LABEL[symbol]} — past 7 days before {asof}\n"
                "<no headlines retrieved for this window>\n")
    else:
        lines = [f"- [{a['seendate'][:8]}] {a['title']}  ({a['domain']})" for a in arts]
        text = (f"## News headlines for {ASSET_LABEL[symbol]} — past 7 days before {asof}\n"
                f"(source: GDELT archive; HEADLINES ONLY, no article bodies)\n"
                + "\n".join(lines) + "\n")
    return text, None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch_news", "cot", "smoke_packs"])
    args = ap.parse_args()
    if args.cmd == "fetch_news":
        fetch_gdelt_all()
    elif args.cmd == "cot":
        df = ensure_cot()
        print(df.tail(3).to_string())
    else:
        for sym in ("GC=F", "NDQ.AX"):
            for fn in (pack_sentiment, pack_fundamental, pack_news):
                text, det = fn(sym, "2024-08-05")
                print(f"=== {fn.__name__} {sym} det③={det}\n{text}")
