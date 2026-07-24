"""rl_eval_signature_v51 —— 假设检验:把 path_v4 决策纪律写进签名,零样本能否修 HOLD 塌陷

MIPROv2 light 在 v5 上 +0.0pp(dspy_optimized_v5.summary.json),分档显示裸签名
几乎不说 HOLD(0.10)。假设:瓶颈是签名缺"显著性闸"纪律,不是措辞。本脚本只换
签名文档字符串(注入 path_v4 政策语义 + 档位基率),同一 dev 300 零样本对比。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research import rl_optimize_prompts_v2 as v2  # noqa: E402
from scripts.research.rl_optimize_prompts_v5 import _load_v5, _metric_v5, TRAINSET  # noqa: E402


def _build_program_v51():
    import dspy

    class CIOSignatureV51(dspy.Signature):
        """根据宏观环境 + 市场技术面 + 用户持仓状态,给出投资建议。

        决策纪律(依次判断):
        1. **显著性闸**:先问"信号强度相对波动率是否显著"。若近期动量/趋势信号
           相对该资产的波动幅度不明确(混杂、弱、互相矛盾),正确答案是 HOLD——
           不动是最常见的正确决策(历史上约 34% 的情形金标是 HOLD)。
        2. **目标仓位思维**:信号显著时,在心中估一个目标仓位(信号越强、波动越低
           → 目标越高)。BUY 仅当目标顶格满仓且当前仓位远低于满仓;SELL 仅当目标
           为清仓(强烈风险信号 + 当前有仓位)。两者都是边界情形,不是默认选项。
        3. **部分调整优先**:目标在中间地带时,用 ACCUMULATE(上调)或 TRIM(下调)
           做部分调整;调整幅度 = 目标与当前仓位的差,不是固定档位。
        4. **成本意识**:小幅度的调整不值手续费——目标与当前仓位很接近时选 HOLD。

        历史金标基率(供校准,不是配额):HOLD 34% / SELL 27% / BUY 18% /
        ACCUMULATE 12% / TRIM 9%。

        verdict 5 选 1:
          - BUY: 一次建满仓(强 bullish + 低波动 + 低仓位)
          - ACCUMULATE: 分批加仓(信号偏多但不足以满仓)
          - HOLD: 不动(信号不显著,或目标仓位≈当前仓位)
          - TRIM: 部分减仓(信号偏空或波动升高,但不足以清仓)
          - SELL: 全部清仓(强风险信号 + 有仓位)

        alloc_pct_of_dry_powder: -1.0 ~ +1.0(正=加仓占子弹比例,负=减仓占持仓比例,HOLD=0)
        """
        macro_context: str = dspy.InputField(desc="VIX/TNX/DXY/USDCNY 4 个宏观指标")
        market_context: str = dspy.InputField(desc="REGIME/RSI/MA/分位/动量")
        portfolio_state: str = dspy.InputField(
            desc="cash% + asset% + concentration% + PnL% + solvency_buffer level")
        reasoning: str = dspy.OutputField(desc="先判显著性,再给目标仓位,最后落 verdict")
        verdict: str = dspy.OutputField(desc="BUY | ACCUMULATE | HOLD | TRIM | SELL")
        alloc_pct_of_dry_powder: float = dspy.OutputField(desc="-1.0 ~ +1.0")

    class CIOPredictorV51(dspy.Module):
        def __init__(self):
            super().__init__()
            self.predict = dspy.ChainOfThought(CIOSignatureV51)

        def forward(self, macro_context, market_context, portfolio_state):
            return self.predict(macro_context=macro_context,
                                market_context=market_context,
                                portfolio_state=portfolio_state)

    return CIOPredictorV51()


def main() -> None:
    v2._configure_dspy()
    examples = _load_v5(TRAINSET)
    train_set, dev_set = v2._stratified_temporal_split(examples, train_ratio=0.8, seed=42)
    # 与 v5 正式跑同款 dev cap(每档最近 60,seed 42)——保证与 summary 可直接对比
    from collections import defaultdict
    import random
    bucket = defaultdict(list)
    for ex in dev_set:
        bucket[ex.verdict].append(ex)
    capped = []
    for vd in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        capped.extend(bucket.get(vd, [])[-60:])
    random.Random(42).shuffle(capped)
    dev_set = capped
    print(f"dev {len(dev_set)}(与 v5 正式跑同构)")

    program = _build_program_v51()
    avg, breakdown = v2._eval_program(program, dev_set, _metric_v5, "v51-zero-shot")
    out = {
        "signature": "v51(path_v4 决策纪律注入)",
        "avg_score": round(avg, 6),
        "per_verdict": {k: {"n": v["n"], "acc": round(v["avg_score"], 4)}
                        for k, v in breakdown.items()},
        "baseline_ref": "dspy_optimized_v5.summary.json(0.4117)",
    }
    p = ROOT / "experiments" / "dspy_signature_v51.eval.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
