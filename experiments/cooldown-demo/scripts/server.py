"""冷静期演示站：一个页面 + 两个接口，只暴露 cooldown.debate_summary()。

不暴露 openinvest 的 MCP / web_api（那些出口都带裁决）。只绑 127.0.0.1，公网经反代进来：
限频按 CF-Connecting-IP（经 Cloudflare 时可信；直连时是 socket 地址）。

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

_committee_lock = threading.Lock()   # 同一时刻只跑一个委员会，其余排队（同 MCP 的 _COMMITTEE_LOCK）
_state_lock = threading.Lock()
# ponytail: 任务和计数都在进程内存，重启清零；要跨重启保留再落 sqlite
_jobs: Dict[str, Dict[str, Any]] = {}
_used: Counter = Counter()
_day = {"d": date.today()}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _take_quota(ip: str) -> str | None:
    """计一次；超限返回原因。按自然日清零。"""
    with _state_lock:
        if _day["d"] != date.today():
            _used.clear()
            _day["d"] = date.today()
        if _used["*"] >= GLOBAL_DAILY:
            return "今天的演示名额用完了，明天再来"
        if _used[ip] >= PER_IP_DAILY:
            return f"每人每天最多 {PER_IP_DAILY} 次，明天再来"
        _used["*"] += 1
        _used[ip] += 1
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
        except Exception as e:  # noqa: BLE001  任何失败都只回一句话，不把栈/内部文本带出去
            print(f"[cooldown] {job_id} failed: {type(e).__name__}: {e}")
            res = {"status": "error", "error": "这次辩论没跑成，稍后再试"}
    _jobs[job_id].update(status="done", result=res)


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
    facts = fund_facts(code)          # 先确认基金存在，再计次、再花 LLM
    if facts is None:
        raise HTTPException(404, "没找到这只基金，确认一下代码")
    reason = _take_quota(_client_ip(request))
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
        out["result"] = job["result"]   # 白名单：cooldown 只会给 {symbol, debate_summary} 或 {status, error}
    return out
