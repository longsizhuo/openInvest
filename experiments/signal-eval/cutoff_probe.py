"""cutoff_probe — 经验探 deepseek-v4-flash 的 effective 训练 cutoff(M3 第 0 步前置闸)。

为什么必须做:arXiv:2403.12958 "Dated Data" 证 effective cutoff 常晚于自报。我们的 holdout
起点 2025-01-01 是按【旧 MiMo】cutoff 2024-12-31 定的。已切 DeepSeek-v4-flash —— 若它 effective
cutoff ≥ 2025-01,则委员会在 2025+ 的回测在【泄漏】(模型见过结果),holdout 边界失锚,
M3 那 ~1080 次花钱全废,连之前那轮 DeepSeek CI 的"干净"也要打问号。

做法:问一串【可精确定日期】的事实(知识阶梯),看它在哪一档停止正确作答。它能稳定答对的
最晚日期 = effective cutoff 的下界。只是 effective 行为下界,非真训练数据 cutoff(无法从外部精确知)。

跑法:uv run --with(无需)python experiments/signal-eval/cutoff_probe.py(需 .env 的 DEEPSEEK_API_KEY)。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from eval_config import OUT_DIR  # noqa: E402

# (锚定日期, 问题, 命中关键词任一即算"知道")。按时间升序。
# 全部用【非政治、可精确定日期】的科技/科学事实,避开 DeepSeek 政治 guardrail 误判成"不知道"。
# 优先用 DeepSeek 自家模型时间线(最可靠、最不会被过滤)。
LADDER = [
    ("2024-09", "Which iPhone model did Apple announce in September 2024, the successor to the iPhone 15?",
     ["iphone 16", "iphone16"]),
    ("2024-10", "The 2024 Nobel Prize in Physics was awarded for foundational work on machine learning "
                "with artificial neural networks. Name one of the two laureates.",
     ["hinton", "hopfield"]),
    ("2024-12", "Which open-source large language model did the company DeepSeek release in December 2024?",
     ["v3", "deepseek-v3", "deepseek v3"]),
    ("2025-01", "Which open-source reasoning model did the company DeepSeek release in January 2025?",
     ["r1", "deepseek-r1", "deepseek r1"]),
    ("2025-05", "Which AI model family did Anthropic release in May 2025 (the Claude 4 generation, "
                "e.g. Opus and Sonnet)?",
     ["claude 4", "opus 4", "sonnet 4", "claude opus 4", "claude-4"]),
    ("2025-08", "Which flagship large language model did OpenAI release in August 2025?",
     ["gpt-5", "gpt 5", "gpt5"]),
]


def _client():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    from openai import OpenAI
    key = os.environ["DEEPSEEK_API_KEY"]
    return OpenAI(api_key=key, base_url="https://api.deepseek.com")


def main() -> dict:
    client = _client()
    results = []
    for anchor, q, hits in LADDER:
        try:
            r = client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user",
                           "content": q + " Answer in one short sentence. If you do not know, say 'I don't know'."}],
                max_tokens=200, temperature=0.0,
                extra_body={"thinking": {"type": "disabled"}},  # 否则 thinking 吃光 token → content 空
            )
            ans = (r.choices[0].message.content or "").strip()
            if not ans:
                ans = "[BLANK/refused]"
        except Exception as e:  # noqa: BLE001
            ans = f"[ERROR {type(e).__name__}: {e}]"
        low = ans.lower()
        knows = any(h in low for h in hits)
        results.append({"anchor": anchor, "knows": knows, "answer": ans[:160]})
        print(f"  {anchor}  {'✓ 知道' if knows else '✗ 不知道'}  {ans[:90]}")

    known = [r["anchor"] for r in results if r["knows"]]
    effective_cutoff_lb = max(known) if known else "<2024-07"
    holdout_start = "2025-01-01"
    leaks = effective_cutoff_lb >= "2025-01"
    verdict = {
        "probe": "deepseek-v4-flash effective cutoff",
        "effective_cutoff_lower_bound": effective_cutoff_lb,
        "holdout_start": holdout_start,
        "holdout_leaks_if_true": leaks,
        "results": results,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "cutoff_probe.json"), "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "decision_log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"gate": "M3_step0_cutoff_probe",
                            "effective_cutoff_lb": effective_cutoff_lb,
                            "holdout_2025_leaks": leaks}, ensure_ascii=False) + "\n")
    print(f"\n→ effective cutoff 下界 ≈ {effective_cutoff_lb}; holdout({holdout_start}) "
          f"{'泄漏 ⚠ 须重锚/换更早窗' if leaks else '暂干净'}")
    return verdict


if __name__ == "__main__":
    main()
