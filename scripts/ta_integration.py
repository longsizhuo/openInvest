"""test_ta Phase B 集成层 — 把 TradingAgents 式分析师挂进委员会（flag 控制）。

- env `INVEST_TA_ANALYSTS`="sentiment,fundamental,news"（逗号子集）→ 开实验臂；
  未设/空 = 关（arm A，与 main 行为等价，有等价性测试守门）
- env `INVEST_TA_ASOF`=YYYY-MM-DD → 数据包钉到历史日（backtest 每日设置）；
  缺省今天（live）
- 分析师只出报告不投票：不进 _check_convergence、不动 parse_cio_memo，
  报告文本追加进 CIO brief（与 Phase A 同 prompt/同温度，唯一变量=集成）
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run_ta_analysts(symbol: str, flag: str) -> Dict[str, str]:
    """跑 flag 指定的分析师，返回 {analyst: report_text}。单个失败跳过不阻断。"""
    from scripts.ta_data import ASSET_LABEL
    from scripts.ta_phase_a import PACK_FN, PROMPTS, _llm_call

    wanted = [a.strip() for a in flag.split(",") if a.strip() in PACK_FN]
    if not wanted or symbol not in ASSET_LABEL:
        return {}
    asof = os.getenv("INVEST_TA_ASOF") or _date.today().isoformat()

    def _one(analyst: str):
        pack, _det = PACK_FN[analyst](symbol, asof)
        prompt = PROMPTS[analyst].format(
            label=ASSET_LABEL[symbol], asof=asof, pack=pack)
        text, _i, _o = _llm_call(prompt)
        return analyst, text

    out: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        for fut in [pool.submit(_one, a) for a in wanted]:
            try:
                name, text = fut.result()
                out[name] = text
            except Exception:  # noqa: BLE001  单分析师失败不阻断委员会
                continue
    return out
