"""重新生成包内默认查表 src/openinvest/jobs/confidence_lookup_default.json（D10 P1）。

只用前瞻纸面舰队（memory/.backtest/ 的前瞻部分，verdict_review.review_fleet 的口径），
**绝不用 live**：这个文件随包发给所有安装，给本机某个 verdict 样本 n<30 时兜底。
内容只有按 verdict 的聚合计数 / 比例 + 市场横盘基率，没有标的、没有日期明细。
行情只读 market_data.db，不触网。

    uv run python -m scripts.gen_confidence_lookup_default
"""
from __future__ import annotations

import json
from datetime import date

from openinvest.jobs.review_calc import build_confidence_lookup
from openinvest.jobs.verdict_review import DEFAULT_LOOKUP_PATH, review_fleet


def main() -> None:
    data = build_confidence_lookup([], review_fleet())
    assert all(c["n_live"] == 0 for c in data["by_verdict"].values())   # 只含舰队
    data["source"] = "fleet"
    data["generated_on"] = date.today().isoformat()
    DEFAULT_LOOKUP_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
