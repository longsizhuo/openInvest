"""_helpers —— skill 子命令共享的小工具

逐字搬运自 scripts/skill.py：
- _safe_close：拉单 symbol 最新收盘价（本地副本，与 services/skill_views.py 的
  同名函数各自独立，互不影响）。
- _print_json：直写真实 stdout 输出 JSON（绕过 utils/* 的 print noise），全部
  子命令都依赖。
- _now_iso_local：本地时区 ISO 时间戳。注意此副本在原 skill.py 内是 DEAD CODE
  （全仓只有 core/portfolio_manager.py 那份在被调用），这里随包搬过来仅为保持
  façade 符号面不变。
"""
from __future__ import annotations

import json
import sys
from typing import Any

__all__ = ["_safe_close", "_print_json", "_now_iso_local"]


def _safe_close(symbol: str) -> float:
    from openinvest.utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


def _print_json(obj: Any) -> None:
    """直接写到原始 stdout，避免被 utils/* 的 print noise 污染"""
    real_stdout = getattr(sys, "__stdout__", sys.stdout)
    real_stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    real_stdout.write("\n")
    real_stdout.flush()


def _now_iso_local():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
