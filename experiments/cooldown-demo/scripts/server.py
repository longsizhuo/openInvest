"""多空对照演示站：一个页面 + 两个接口，只暴露 cooldown.debate_summary() 的骨架和程序算的数据。

不暴露 openinvest 的 MCP / web_api（那些出口都带裁决）。只绑 127.0.0.1，公网经反代进来。
限频认 X-Client-IP：反代必须用自己算出的客户端地址**覆盖**这个头（Caddy：
`header_up X-Client-IP {client_ip}`，经 Cloudflare 时配 trusted_proxies），客户端自带的会被覆盖，伪造不了。

页面上的每个数字都来自这里的计算，不来自模型：模型只给一句话结论和论点短语，
论点引用的指标键在 _run 里按 metrics 回填数值。

    INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 \
        uv run uvicorn --app-dir experiments/cooldown-demo/scripts server:app --port 8769
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

import cooldown

# 一次委员会约 ¥0.01（deepseek-v4-flash，2026-10 实测），全站 100 次/天 ≈ ¥1/天封顶
PER_IP_DAILY = int(os.getenv("COOLDOWN_PER_IP_DAILY", "3"))
GLOBAL_DAILY = int(os.getenv("COOLDOWN_GLOBAL_DAILY", "100"))
# 查基金（下载东方财富净值）另算一份更宽的额度，在下载之前扣——查不到的代码也算
LOOKUP_PER_IP_DAILY = int(os.getenv("COOLDOWN_LOOKUP_PER_IP_DAILY", "30"))
LOOKUP_GLOBAL_DAILY = int(os.getenv("COOLDOWN_LOOKUP_GLOBAL_DAILY", "2000"))
GENERIC_ERROR = "这次辩论没跑成，稍后再试"

_committee_lock = threading.Lock()   # 同一时刻只跑一个委员会，其余排队（同 MCP 的 _COMMITTEE_LOCK）
_state_lock = threading.Lock()
# ponytail: 任务和计数都在进程内存，重启清零；要跨重启保留再落 sqlite
_jobs: Dict[str, Dict[str, Any]] = {}
_used: Counter = Counter()          # 辩论次数：ip / "*"
_lookups: Counter = Counter()       # 查基金次数：ip / "*"
_facts_cache: Dict[str, Any] = {}   # code → 当天的 fund_facts 结果（含 None），跨日清空
_day = {"d": date.today()}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _client_ip(request: Request) -> str:
    return request.headers.get("x-client-ip") or (request.client.host if request.client else "?")


def _take(counter: Counter, ip: str, per_ip: int, total: int, what: str) -> str | None:
    """计一次；超限返回原因。按自然日清零（所有计数和缓存一起清）。"""
    with _state_lock:
        if _day["d"] != date.today():
            _used.clear()
            _lookups.clear()
            _facts_cache.clear()
            _day["d"] = date.today()
        if counter["*"] >= total:
            return f"今天的{what}名额用完了，明天再来"
        if counter[ip] >= per_ip:
            return f"每人每天最多{what} {per_ip} 次，明天再来"
        counter["*"] += 1
        counter[ip] += 1
        return None


# ---------- 数据：全部由程序计算 ----------

def _pct_rank(series, value) -> int:
    """value 在 series 里的分位（0~100，含等于）。"""
    return round(float((series <= value).mean()) * 100)


def _pingzhong(code: str) -> Dict[str, Any]:
    """东方财富 pingzhongdata 里页面要用的字段：基金名、阶段收益、规模、股票仓位。拿不到就缺。"""
    try:
        text = requests.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", timeout=10,
                            headers={"Referer": "https://fundf10.eastmoney.com/"}).text
    except requests.RequestException:
        return {}

    def var(name: str) -> Any:
        m = re.search(rf"var\s+{name}\s*=\s*(.*?);", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None

    out: Dict[str, Any] = {"name": var("fS_name")}
    for key, name in (("ret_1m", "syl_1y"), ("ret_3m", "syl_3y"), ("ret_6m", "syl_6y"), ("ret_1y", "syl_1n")):
        try:
            out[key] = float(var(name))
        except (TypeError, ValueError):
            pass
    scale = var("Data_fluctuationScale") or {}
    pts = [p.get("y") for p in scale.get("series") or [] if isinstance(p, dict)]
    if len(pts) >= 2 and pts[-1] and pts[max(0, len(pts) - 5)]:
        out["scale_now"], out["scale_prev"] = float(pts[-1]), float(pts[max(0, len(pts) - 5)])
    alloc = var("Data_assetAllocation") or {}
    for s in alloc.get("series") or []:
        if isinstance(s, dict) and s.get("name") == "股票占净比" and len(s.get("data") or []) >= 2:
            out["stock_now"], out["stock_prev"] = float(s["data"][-1]), float(s["data"][0])
    return out


def _metrics(d: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """指标键 → {text, chip, hot}。hot 按固定阈值（页面“标红规则”），不由模型决定。"""
    m: Dict[str, Dict[str, Any]] = {}

    def put(key: str, text: str, hot: bool = False, chip: Optional[str] = None) -> None:
        m[key] = {"text": text, "hot": hot, "chip": chip or text}

    sign = lambda v: f"{v:+.1f}%".replace("+-", "-")
    if "ret_1y" in d:
        put("ret_1y", f"近 1 年 {sign(d['ret_1y'])}", d["ret_1y"] <= -20)
    if "ret_6m" in d:
        put("ret_6m", f"近 6 月 {sign(d['ret_6m'])}", d["ret_6m"] <= -15)
    if "scale_now" in d:
        chg = (d["scale_now"] / d["scale_prev"] - 1) * 100
        put("scale", f"规模一年 {chg:+.0f}%", chg <= -30)
    if "stock_now" in d:
        delta = d["stock_now"] - d["stock_prev"]
        put("stock", f"股票仓位 {d['stock_prev']:.1f}% → {d['stock_now']:.1f}%", abs(delta) >= 10,
            f"股票仓位 {delta:+.1f} 个百分点")
    put("drawdown", f"两年最深 {d['mdd']:.1f}%", d["mdd"] <= -20)
    put("from_high", f"距两年高点 {d['from_high']:.1f}%", d["from_high"] <= -20)
    put("price_pct", f"两年价格分位 {d['price_pct']}%")
    put("trend", f"MA20 {'低于' if d['ma_spread'] < 0 else '高于'} MA120 {abs(d['ma_spread']):.1f}%")
    put("rsi", f"RSI {d['rsi']:.1f}")
    if "vix" in d:
        put("vix", f"VIX {d['vix']:.2f}，两年分位 {d['vix_pct']}%", d["vix_pct"] >= 90,
            f"VIX {d['vix_pct']}% 分位")
    if "tnx" in d:
        put("tnx", f"10Y 美债 {d['tnx']:.2f}%，两年分位 {d['tnx_pct']}%", d["tnx_pct"] >= 90,
            f"美债收益率 {d['tnx_pct']}% 分位")
    return m


# 顶部最多三个标红数字的优先级：用户最在乎的放前面
_CHIP_ORDER = ("ret_1y", "scale", "tnx", "from_high", "drawdown", "ret_6m", "stock", "vix")


def fund_facts(code: str) -> Dict[str, Any] | None:
    """页面的全部确定性数据。查不到这只基金返回 None。"""
    import pandas as pd
    from openinvest.calc.market_metrics import METRICS_PERIOD, compute_metrics
    from openinvest.calc.regime import classify_regime
    from openinvest.core.regime_probability import build_reentry_reference
    from openinvest.utils.exchange_fee import get_history_data

    symbol = f"FUND:{code}"
    full = get_history_data(symbol, "max")
    if full is None or full.empty or len(full) < 30:
        return None
    c = full["Close"]
    cut = c.index[-1] - pd.DateOffset(years=2)
    two = c[c.index > cut]
    dd = two / two.cummax() - 1
    trough = dd.idxmin()
    peak = two.loc[:trough].idxmax()
    ma = {n: c.rolling(n).mean() for n in (20, 60, 120, 250)}
    diff = c.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn)
    ret = c.pct_change() * 100
    idx = two.index
    r3 = lambda s: [None if v != v else round(float(v), 4) for v in s.reindex(idx)]
    chart = {"d": [i.strftime("%Y-%m-%d") for i in idx], "c": r3(c),
             **{f"ma{n}": r3(ma[n]) for n in ma},
             "ret": [round(float(v), 2) if v == v else 0.0 for v in ret.reindex(idx)],
             "rsi": [round(float(v), 1) if v == v else 50.0 for v in rsi.reindex(idx)],
             "peak": peak.strftime("%Y-%m-%d"), "trough": trough.strftime("%Y-%m-%d"),
             "mdd": round(float(dd.min()) * 100, 1)}

    data: Dict[str, Any] = {
        "mdd": float(dd.min()) * 100, "from_high": float(dd.iloc[-1]) * 100,
        "price_pct": _pct_rank(two, two.iloc[-1]),
        "ma_spread": float(ma[20].iloc[-1] / ma[120].iloc[-1] - 1) * 100,
        "rsi": float(rsi.iloc[-1]),
    }
    for sym, key in (("^VIX", "vix"), ("^TNX", "tnx")):
        try:
            s = get_history_data(sym, "2y")["Close"].dropna()
            data[key], data[f"{key}_pct"] = float(s.iloc[-1]), _pct_rank(s, s.iloc[-1])
        except Exception:  # noqa: BLE001  宏观指标拿不到就不给这一项
            pass
    pz = _pingzhong(code)
    data.update({k: v for k, v in pz.items() if k != "name"})
    metrics = _metrics(data)
    chips = [metrics[k]["chip"] for k in _CHIP_ORDER if k in metrics and metrics[k]["hot"]][:3]

    dist: List[Dict[str, Any]] = []
    try:
        regime = classify_regime(compute_metrics(get_history_data(symbol, METRICS_PERIOD)), symbol)["regime"]
        _, profile = build_reentry_reference(symbol, regime, float(c.iloc[-1]))
        for w, label in (("30d", "30 天"), ("60d", "60 天"), ("90d", "90 天")):
            x = ((profile or {}).get("windows") or {}).get(w)
            if x:
                dist.append({"w": label, "below": round(x["p_below"] * 100), "p10": round(x["p10_pct"], 1),
                             "med": round(x["median_pct"], 1), "p90": round(x["p90_pct"], 1),
                             "eff": x.get("effective_n"), "low": bool(x.get("low_confidence"))})
    except Exception:  # noqa: BLE001  分布拿不到就不显示这一块
        pass

    return {"name": pz.get("name") or code, "code": code,
            "nav": round(float(c.iloc[-1]), 4), "nav_date": c.index[-1].strftime("%Y-%m-%d"),
            "chips": chips, "chart": chart, "dist": dist, "metrics": metrics}


# ---------- 委员会 ----------

def _run(job_id: str, code: str) -> None:
    metrics = _jobs[job_id]["facts"]["metrics"]
    with _committee_lock:
        _jobs[job_id]["status"] = "running"
        try:
            res = cooldown.debate_summary(f"FUND:{code}", {k: v["text"] for k, v in metrics.items()})
        except Exception as e:  # noqa: BLE001
            res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    # 出口白名单：只放行书记员骨架，依据数值按指标键从 metrics 回填；其余一律通用一句话——
    # 后端报错可能带内部 URL / 路径，只进服务端日志
    if res.get("headline"):
        side = lambda k: [{"role": it["role"], "claim": it["claim"],
                           "evidence": (metrics.get(it["metric"]) or {}).get("text", ""),
                           "hot": bool((metrics.get(it["metric"]) or {}).get("hot"))}
                          for it in res.get(k) or []]
        out = {"headline": res["headline"], "pros": side("pros"), "cons": side("cons")}
    else:
        print(f"[cooldown] {job_id} {code} failed: {res.get('error')}")
        out = {"status": "error", "error": GENERIC_ERROR}
    _jobs[job_id].update(status="done", result=out)


class DebateIn(BaseModel):
    code: str


def _public(facts: Dict[str, Any]) -> Dict[str, Any]:
    """给页面的数据：metrics 只是回填用的内部表，不单独下发。"""
    return {k: v for k, v in facts.items() if k != "metrics"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("index.html"))


@app.post("/api/debate")
def start(body: DebateIn, request: Request) -> Dict[str, Any]:
    code = body.code.strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(400, "请输入 6 位基金代码")
    ip = _client_ip(request)
    if code not in _facts_cache:      # 同一只基金当天只下载一次
        reason = _take(_lookups, ip, LOOKUP_PER_IP_DAILY, LOOKUP_GLOBAL_DAILY, "查询")
        if reason:
            raise HTTPException(429, reason)
        _facts_cache[code] = fund_facts(code)
    facts = _facts_cache[code]        # 先确认基金存在，再扣辩论次数、再花 LLM
    if facts is None:
        raise HTTPException(404, "没找到这只基金，确认一下代码")
    reason = _take(_used, ip, PER_IP_DAILY, GLOBAL_DAILY, "辩论")
    if reason:
        raise HTTPException(429, reason)
    job_id = uuid.uuid4().hex
    _jobs[job_id] = {"status": "queued", "facts": facts}
    threading.Thread(target=_run, args=(job_id, code), daemon=True).start()
    return {"job_id": job_id, "facts": _public(facts)}


@app.get("/api/debate/{job_id}")
def poll(job_id: str) -> Dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在（服务可能重启过）")
    out = {"status": job["status"]}
    if job["status"] == "done":
        out["result"] = job["result"]   # _run 里按白名单构造过
    return out
