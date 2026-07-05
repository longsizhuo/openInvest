"""周日 11:00 复盘过去一周的建议命中率（占位 - P3 Dreaming 完成后细化）

读 db/jobs.sqlite 的 job_runs 表 + memory/portfolio_history.jsonl，
对比"agent 当时建议"与"实际市场表现"，生成命中率报告。
"""
from __future__ import annotations

from typing import Any, Dict


def run() -> Dict[str, Any]:
    return {"status": "not_implemented", "note": "wait for P3 (Dreaming) to land first"}


if __name__ == "__main__":
    print(run())
