"""冷静期演示站：一个页面 + 两个接口，只暴露 cooldown.debate_summary()。

不暴露 openinvest 的 MCP / web_api（那些出口都带裁决）。只绑 127.0.0.1，公网经反代进来。
限频认 X-Client-IP：反代必须用自己算出的客户端地址**覆盖**这个头（Caddy：
`header_up X-Client-IP {client_ip}`，经 Cloudflare 时配 trusted_proxies），客户端自带的会被覆盖，伪造不了。

    INVEST_HOME=~/openinvest-demo INVEST_ADVISORY_MODE=1 \
        uv run uvicorn --app-dir experiments/cooldown-demo/scripts server:app --port 8769
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict

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


def fund_facts(code: str) -> Dict[str, Any] | None:
    """基金名 + 近两年最大回撤 + 离近两年高点多远。纯计算，不调 LLM；查不到返回 None。"""
    from openinvest.utils.exchange_fee import get_history_data

    df = get_history_data(f"FUND:{code}", "2y")
    if df is None or df.empty:
        return None
    close = df["Close"]
    drawdown = close / close.cummax() - 1
    trough = drawdown.idxmin()
    peak = close.loc[:trough].idxmax()
    picks = list(range(0, len(close), max(1, len(close) // 120)))   # 页面小图约 120 个点够了
    if picks[-1] != len(close) - 1:
        picks.append(len(close) - 1)
    series = close.iloc[picks]
    name = code
    try:
        text = requests.get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js", timeout=10,
                            headers={"Referer": "https://fundf10.eastmoney.com/"}).text
        m = re.search(r'fS_name\s*=\s*"([^"]+)"', text)
        name = m.group(1) if m else code
    except requests.RequestException:
        pass  # 基金名只是展示，拿不到就显示代码
    return {
        "name": name,
        "nav": round(float(close.iloc[-1]), 4),
        "nav_date": close.index[-1].strftime("%Y-%m-%d"),
        "max_drawdown_2y": round(float(drawdown.min()) * 100, 1),
        "from_high_2y": round(float(drawdown.iloc[-1]) * 100, 1),
        "peak": peak.strftime("%Y-%m-%d"),
        "trough": trough.strftime("%Y-%m-%d"),
        "series": [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in series.items()],
    }


def _run(job_id: str, code: str) -> None:
    with _committee_lock:
        _jobs[job_id]["status"] = "running"
        try:
            res = cooldown.debate_summary(f"FUND:{code}")
        except Exception as e:  # noqa: BLE001
            res = {"status": "error", "error": f"{type(e).__name__}: {e}"}
    # 出口白名单：只放行纪要；其余一律通用一句话——后端报错可能带内部 URL / 路径，只进服务端日志
    if res.get("debate_summary"):
        out = {"symbol": res["symbol"], "debate_summary": res["debate_summary"]}
    else:
        print(f"[cooldown] {job_id} {code} failed: {res.get('error')}")
        out = {"status": "error", "error": GENERIC_ERROR}
    _jobs[job_id].update(status="done", result=out)


class DebateIn(BaseModel):
    code: str


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
    return {"job_id": job_id, "facts": facts}


@app.get("/api/debate/{job_id}")
def poll(job_id: str) -> Dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在（服务可能重启过）")
    out = {"status": job["status"], "facts": job["facts"]}
    if job["status"] == "done":
        out["result"] = job["result"]   # _run 里按白名单构造过
    return out
