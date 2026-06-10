"""test_ta Phase A 跑批 — 3 个 TradingAgents 式分析师独立产报告 + 强制 STANCE。

每个 (决策日 × 资产 × 分析师) 一次 LLM 调用（无辩论、无委员会——信号存在性
检验，不测集成）。输出 jsonl 断点续跑（按 key 跳过已完成）。

Prompt 改编自 TauricResearch/TradingAgents（Apache-2.0）的
fundamentals/news/sentiment analyst system messages：保留其"researcher 写
comprehensive report + actionable insights"框架，去掉工具调用/多 agent 协作
段（Phase A 数据已预取在 prompt 里），加强制 STANCE/CONFIDENCE 字段供确定性
解析。LLM 配置与生产委员会同源（utils.llm.get_llm_config_safe）。

用法：
    uv run python scripts/ta_phase_a.py --smoke          # 1 个日期 × 2 资产 × 3 分析师
    uv run python scripts/ta_phase_a.py                  # 全量 732 调用（断点续跑）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# standalone 脚本不经 skill.py 入口 → 必须显式加载 .env（历史坑：LLM key 缺失）
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from scripts.ta_data import (  # noqa: E402
    ASSET_LABEL, CACHE, load_dates, pack_fundamental, pack_news, pack_sentiment,
)

OUT = CACHE / "phase_a_reports.jsonl"

# ---------------------------------------------------------------------------
# Prompts（TradingAgents 改编）
# ---------------------------------------------------------------------------
_COMMON_TAIL = """
## Output requirements (MANDATORY)

Write a concise analyst report (<= 250 words) with specific, actionable insights
and supporting evidence from the data above. Do NOT invent data that is not shown.
Then finish with EXACTLY these three lines (machine-parsed):

STANCE: bullish | bearish | neutral
CONFIDENCE: low | medium | high
ONE_LINER: <one sentence summary of your read>

STANCE is your 30-day directional read for {label} based ONLY on the data above.
Use neutral when the evidence is genuinely mixed or insufficient.
"""

PROMPTS = {
    "fundamental": (
        "You are a researcher tasked with analyzing fundamental information about "
        "{label}. Please write a report of the asset's fundamental information to "
        "gain a full view and inform traders. The relevant fundamental data has "
        "already been collected for you below (point-in-time as of {asof}; for gold "
        "futures the 'fundamentals' are positioning data, since gold has no earnings). "
        "Provide specific, actionable insights with supporting evidence.\n\n{pack}\n"
        + _COMMON_TAIL
    ),
    "news": (
        "You are a news researcher tasked with analyzing recent news and trends over "
        "the past week relevant to {label}. Please write a report of the current "
        "state of the world that is relevant for trading this asset. The news "
        "headlines (past 7 days before {asof}) have already been collected for you "
        "below — HEADLINES ONLY, no article bodies; weigh them accordingly and do "
        "not invent article contents. Provide specific, actionable insights.\n\n{pack}\n"
        + _COMMON_TAIL
    ),
    "sentiment": (
        "You are a financial market sentiment analyst. Your task is to produce a "
        "sentiment report for {label} as of {asof}, drawing on the volatility / "
        "fear-greed data that has already been collected for you below. Read extreme "
        "fear and extreme greed both as direct signals AND as potential contrarian "
        "over-extension — judge which reading fits the evidence. Past sentiment is "
        "not mechanically predictive; weigh sample context.\n\n{pack}\n"
        + _COMMON_TAIL
    ),
}

PACK_FN = {
    "fundamental": pack_fundamental,
    "news": pack_news,
    "sentiment": pack_sentiment,
}

STANCE_RE = re.compile(r"STANCE:\s*(bullish|bearish|neutral)", re.I)
CONF_RE = re.compile(r"CONFIDENCE:\s*(low|medium|high)", re.I)

_write_lock = threading.Lock()
_usage = {"in": 0, "out": 0}


def _llm_call(prompt: str) -> Tuple[str, int, int]:
    from openai import OpenAI
    from utils.llm import get_llm_config_safe

    api_key, base_url, model, _p = get_llm_config_safe(default_model="deepseek-v4-flash")
    client = OpenAI(api_key=api_key, base_url=base_url)
    extra = {}
    if "v4" in (model or ""):
        extra["extra_body"] = {"thinking": {"type": "disabled"}}
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,   # 与委员会 Round1 同档
        **extra,
    )
    usage = getattr(resp, "usage", None)
    return (
        resp.choices[0].message.content or "",
        getattr(usage, "prompt_tokens", 0) or 0,
        getattr(usage, "completion_tokens", 0) or 0,
    )


def run_one(date: str, symbol: str, analyst: str) -> Optional[Dict]:
    pack, det = PACK_FN[analyst](symbol, date)
    prompt = PROMPTS[analyst].format(label=ASSET_LABEL[symbol], asof=date, pack=pack)
    text, tin, tout = _llm_call(prompt)
    m = STANCE_RE.search(text)
    if not m:  # 缺字段重试一次
        text, tin2, tout2 = _llm_call(
            prompt + "\nREMINDER: you MUST end with the three mandatory lines.")
        tin, tout = tin + tin2, tout + tout2
        m = STANCE_RE.search(text)
    cm = CONF_RE.search(text)
    with _write_lock:
        _usage["in"] += tin
        _usage["out"] += tout
    return {
        "key": f"{date}|{symbol}|{analyst}",
        "date": date, "symbol": symbol, "analyst": analyst,
        "stance": m.group(1).lower() if m else None,
        "confidence": cm.group(1).lower() if cm else None,
        "det_stance": det,                # 确定性基线③（news 为 None）
        "report": text,
        "tokens_in": tin, "tokens_out": tout,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="只跑 1 个日期做质检+成本外推")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    dates, assets = load_dates()
    if args.smoke:
        dates = ["2024-08-05"]

    done = set()
    if OUT.exists():
        done = {json.loads(l)["key"] for l in OUT.open() if l.strip()}
    todo = [(d, s, a) for d in dates for s in assets for a in PACK_FN
            if f"{d}|{s}|{a}" not in done]
    print(f"待跑 {len(todo)}（已完成 {len(done)}）")
    if not todo:
        return

    t0 = time.time()
    n_err = 0
    with OUT.open("a") as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_one, d, s, a): (d, s, a) for d, s, a in todo}
        for i, fut in enumerate(as_completed(futs)):
            key = futs[fut]
            try:
                row = fut.result()
                with _write_lock:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
            except Exception as e:  # noqa: BLE001  单条失败不阻断，重跑可续
                n_err += 1
                print(f"  ERR {key}: {type(e).__name__}: {str(e)[:120]}")
            if (i + 1) % 20 == 0:
                print(f"  {i+1}/{len(todo)}  elapsed {time.time()-t0:.0f}s  "
                      f"tokens in/out {_usage['in']}/{_usage['out']}")
    print(f"完成 {len(todo)-n_err}/{len(todo)}，错误 {n_err}，"
          f"tokens in={_usage['in']} out={_usage['out']}，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
