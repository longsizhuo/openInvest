"""基准数据源 - 给 PnL 折线图叠加 vs 沪深 300 / 公募基金 / AI 投顾的对比。

3 类基准：

1. **指数 / ETF（yfinance）** —— 拉历史日线，从 START_DATE 起算累计涨幅 %
2. **常数年化（理财 / 定存 / 余额宝）** —— 写死 APR，画水平直线
3. **公募基金（天天基金 API）** —— 爬 fund.eastmoney.com/pingzhongdata/<code>.js
4. **AI 投顾（搜索得来）** —— 一次性宣传数据，写死 + 标注来源 URL + retrieved_date

设计：
- 数据缓存到 memory/.state/benchmarks/<key>.json，gitignore 保护
- 渲染时同图叠加，用户实盘线粗黄色突出，基准线细灰色 0.4 透明度
- 隐私不变：仍只画 % 趋势，无明文金额
- 网络失败时返回 None，pnl_snapshot 跳过这条 series（不影响 SVG 主体）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yfinance as yf

ROOT = Path(__file__).parent.parent
CACHE_DIR = ROOT / "memory" / ".state" / "benchmarks"


# 基准定义。key 是显示用的标签，写中文方便 SVG 图例。
# source 决定怎么取数；color 是 SVG 线条颜色（GitHub dark mode 友好）；group 用于分类
BENCHMARKS: Dict[str, Dict[str, Any]] = {
    # === Tier 1: 指数 ===
    "沪深300": {
        "source": "yfinance", "symbol": "000300.SS",
        "color": "#e3b341", "group": "index", "dash": "1 0",
    },
    "标普500": {
        "source": "yfinance", "symbol": "^GSPC",
        "color": "#a371f7", "group": "index", "dash": "1 0",
    },
    "纳指100": {
        "source": "yfinance", "symbol": "^NDX",
        "color": "#ff7b72", "group": "index", "dash": "1 0",
    },
    # === Tier 1: 常数年化（理财基线，水平虚线）===
    "余额宝 (1.3%)": {
        "source": "constant_apr", "apr_pct": 1.3,
        "color": "#7d8590", "group": "savings", "dash": "3 3",
    },
    "1 年定存 (1.5%)": {
        "source": "constant_apr", "apr_pct": 1.5,
        "color": "#6e7681", "group": "savings", "dash": "3 3",
    },
    # === Tier 2: 公募基金 ===
    "易方达蓝筹 005827": {
        "source": "eastmoney_fund", "code": "005827",
        "color": "#3fb950", "group": "fund", "dash": "5 2",
    },
    "兴全合宜 163417": {
        "source": "eastmoney_fund", "code": "163417",
        "color": "#56d364", "group": "fund", "dash": "5 2",
    },
    "招商白酒 161725": {
        "source": "eastmoney_fund", "code": "161725",
        "color": "#2ea043", "group": "fund", "dash": "5 2",
    },
    # === Tier 3: AI 投顾（一次性搜索得来，标注 source + retrieved date）===
    "Wealthfront (年化 6.2%)": {
        "source": "constant_apr", "apr_pct": 6.2,
        "color": "#58a6ff", "group": "ai_advisor", "dash": "3 3",
        "_meta": {
            "note": "Wealthfront 公开历史年化均值，来自 NerdWallet 2025 搜索",
            "retrieved": "2026-04-28",
            "source_url": "https://tokenist.com/investing/betterment-vs-wealthfront/",
        },
    },
    "Betterment (年化 6.1%)": {
        "source": "constant_apr", "apr_pct": 6.1,
        "color": "#79c0ff", "group": "ai_advisor", "dash": "3 3",
        "_meta": {
            "note": "Betterment 公开历史年化均值，同上来源",
            "retrieved": "2026-04-28",
            "source_url": "https://tokenist.com/investing/betterment-vs-wealthfront/",
        },
    },
    "蚂蚁帮你投 (年化 5.16%)": {
        "source": "constant_apr", "apr_pct": 5.16,
        "color": "#bc8cff", "group": "ai_advisor", "dash": "3 3",
        "_meta": {
            "note": "蚂蚁财富与 Vanguard 合作'帮你投'平衡方案 2022Q2 披露收益",
            "retrieved": "2026-04-28",
            "source_url": "https://zhuanlan.zhihu.com/p/128638957",
        },
    },
}


@dataclass
class BenchmarkSeries:
    """一条基准的时间序列：每个 key 是 YYYY-MM-DD，value 是相对 start_date 的累计涨幅 %"""
    key: str
    color: str
    group: str
    dash: str
    points: Dict[str, float]  # {"2026-04-28": 12.34, ...}


# ---------- 各 source 的取数 helper ----------

def _fetch_yfinance(symbol: str, start: str, end: str) -> Dict[str, float]:
    """{date_str: close}。NaN 收盘价用前一个有效值兜底。"""
    df = yf.Ticker(symbol).history(start=start, end=end)
    if df.empty:
        return {}
    out: Dict[str, float] = {}
    last_valid: Optional[float] = None
    for idx, row in df.iterrows():
        v = row["Close"]
        if v is None or (isinstance(v, float) and (v != v)):  # NaN check
            if last_valid is None:
                continue
            out[idx.strftime("%Y-%m-%d")] = last_valid
        else:
            v = float(v)
            out[idx.strftime("%Y-%m-%d")] = v
            last_valid = v
    return out


_PINGZHONG_RE = re.compile(r"Data_netWorthTrend\s*=\s*(\[[^;]+?\]);", re.DOTALL)


def _fetch_eastmoney_fund(code: str, start: str, end: str) -> Dict[str, float]:
    """爬天天基金 pingzhong API 拿历史净值。

    返回 {date: 单位净值}。该 endpoint 数据从基金成立日起，覆盖完整历史。
    """
    url = f"http://fund.eastmoney.com/pingzhongdata/{code}.js"
    headers = {
        "Referer": "http://fundf10.eastmoney.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"⚠️ eastmoney fund {code} 拉取失败: {e}")
        return {}

    m = _PINGZHONG_RE.search(resp.text)
    if not m:
        print(f"⚠️ eastmoney fund {code}: Data_netWorthTrend 字段未找到")
        return {}

    # JS array 大致是 [{"x":1234567890000,"y":1.0234,...}, ...]
    try:
        # 用 eval 太危险，手工解析关键字段
        items = re.findall(r'\{"x":(\d+),"y":([\d.]+)', m.group(1))
    except Exception as e:
        print(f"⚠️ eastmoney fund {code} 解析失败: {e}")
        return {}

    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    out: Dict[str, float] = {}
    for ts_ms, nav in items:
        d = datetime.fromtimestamp(int(ts_ms) / 1000)
        if start_dt <= d <= end_dt:
            out[d.strftime("%Y-%m-%d")] = float(nav)
    return out


def _generate_constant_apr(
    apr_pct: float, start: str, end: str
) -> Dict[str, float]:
    """常数年化 → 模拟一个净值序列：start 日 1.0，按日累计复利"""
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    daily_rate = (1 + apr_pct / 100) ** (1 / 365) - 1
    out: Dict[str, float] = {}
    cur = start_dt
    nav = 1.0
    while cur <= end_dt:
        out[cur.strftime("%Y-%m-%d")] = nav
        nav *= (1 + daily_rate)
        cur += timedelta(days=1)
    return out


# ---------- 缓存层 ----------

def _cache_path(key: str) -> Path:
    safe_key = re.sub(r"[^\w一-鿿]+", "_", key)
    return CACHE_DIR / f"{safe_key}.json"


def refresh_benchmark(key: str, start: str, end: str) -> Optional[Dict[str, Any]]:
    """拉数据 + 写缓存。返回 cached payload 或 None"""
    if key not in BENCHMARKS:
        return None
    config = BENCHMARKS[key]
    source = config["source"]

    if source == "yfinance":
        prices = _fetch_yfinance(config["symbol"], start, end)
    elif source == "eastmoney_fund":
        prices = _fetch_eastmoney_fund(config["code"], start, end)
    elif source == "constant_apr":
        prices = _generate_constant_apr(config["apr_pct"], start, end)
    else:
        return None

    if not prices:
        return None

    payload = {
        "key": key,
        "source": source,
        "config": {k: v for k, v in config.items() if not k.startswith("_")},
        "_meta": config.get("_meta"),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "start": start,
        "end": end,
        "prices": prices,  # {YYYY-MM-DD: nav_or_close}
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(key), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def load_benchmark(key: str) -> Optional[Dict[str, Any]]:
    """从缓存读，没缓存返回 None（调用方自己决定要不要 refresh）"""
    p = _cache_path(key)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def to_pct_series(prices: Dict[str, float], start_date: str) -> Dict[str, float]:
    """把绝对价 series 转成"相对 start_date 的累计涨幅 %"。

    第一个点 0%，之后每天 (price / start_price - 1) * 100。
    没有 start_date 当天数据就用最早能找到的有效价当 baseline。
    """
    if not prices:
        return {}
    # 找 baseline：start_date 当天，没有就用最早的
    sorted_dates = sorted(prices.keys())
    baseline_date = start_date if start_date in prices else next(
        (d for d in sorted_dates if d >= start_date), sorted_dates[0]
    )
    baseline = prices[baseline_date]
    if baseline <= 0:
        return {}
    return {d: ((p / baseline) - 1) * 100 for d, p in prices.items() if d >= baseline_date}


def get_all_series(start_date: str) -> List[BenchmarkSeries]:
    """加载所有缓存的基准，转成 BenchmarkSeries 列表（给 SVG 渲染用）"""
    out: List[BenchmarkSeries] = []
    for key, config in BENCHMARKS.items():
        cached = load_benchmark(key)
        if not cached:
            continue
        pct_series = to_pct_series(cached["prices"], start_date)
        if not pct_series:
            continue
        out.append(BenchmarkSeries(
            key=key,
            color=config["color"],
            group=config["group"],
            dash=config["dash"],
            points=pct_series,
        ))
    return out


__all__ = [
    "BENCHMARKS",
    "BenchmarkSeries",
    "refresh_benchmark",
    "load_benchmark",
    "to_pct_series",
    "get_all_series",
]
