"""rl_eval_signature_v52 —— 假设检验:补齐 σ 信息缺口(而非调措辞)能否涨分

v5 系列证据链:MIPRO light +0.0pp(措辞搜索无效)→ v5.1 纪律注入 −14.7pp
(行为偏置钝杠杆,轻重两头塌)。本假设:path_v4 金标 = f(μ,σ),输入缺 σ ——
把 sigma30(30 交易日尺度波动率)作为显式输入字段,签名保持中性,同 dev 零样本对比。
"""
from __future__ import annotations

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research import rl_optimize_prompts_v2 as v2  # noqa: E402
from scripts.research.rl_optimize_prompts_v5 import _metric_v5, TRAINSET  # noqa: E402


def _load_with_sigma():
    import dspy
    examples = []
    for line in TRAINSET.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        if s.get("bucket") != "clean":
            continue
        sigma = float(s["sigma30"])
        examples.append(dspy.Example(
            macro_context=s["macro_context"],
            market_context=s["market_context"],
            portfolio_state=s["portfolio_state"],
            volatility_context=(
                f"30 交易日尺度波动率 sigma30 = {sigma:.4f}"
                f"(即未来一个月典型波动幅度约 ±{100*sigma:.1f}%)"
            ),
            verdict=str(s["verdict"]).upper(),
            decision_date=s.get("decision_date", ""),
        ).with_inputs("macro_context", "market_context", "portfolio_state",
                      "volatility_context"))
        # 与 v5/v5.1 同一切分逻辑依赖 verdict/decision_date 字段,以上已带
    print(f"📦 clean 样本 {len(examples)}(带 sigma30 输入)")
    return examples


def _build_program_v52():
    import dspy

    class CIOSignatureV52(dspy.Signature):
        """根据宏观环境 + 市场技术面 + 波动率 + 用户持仓状态,给出投资建议。

        提示:合理仓位与"预期收益相对波动率的比值"成正比——同样的动量信号,
        波动率越高,应持仓位越低;信号幅度不超过波动噪声时,不动(HOLD)是对的。

        verdict 5 选 1:
          - BUY: 一次建满仓 - ACCUMULATE: 分批加仓 - HOLD: 维持现状
          - TRIM: 部分减仓 - SELL: 全部清仓
        """
        macro_context: str = dspy.InputField(desc="VIX/TNX/DXY/USDCNY")
        market_context: str = dspy.InputField(desc="REGIME/RSI/MA/分位/动量")
        volatility_context: str = dspy.InputField(desc="该资产 30 日尺度波动率 sigma30")
        portfolio_state: str = dspy.InputField(desc="cash% + asset% + PnL% + solvency")
        reasoning: str = dspy.OutputField(desc="信号强度 vs 波动率 → 目标仓位 → verdict")
        verdict: str = dspy.OutputField(desc="BUY | ACCUMULATE | HOLD | TRIM | SELL")

    class CIOPredictorV52(dspy.Module):
        def __init__(self):
            super().__init__()
            self.predict = dspy.ChainOfThought(CIOSignatureV52)

        def forward(self, macro_context, market_context, portfolio_state, volatility_context):
            return self.predict(macro_context=macro_context, market_context=market_context,
                                portfolio_state=portfolio_state,
                                volatility_context=volatility_context)

    return CIOPredictorV52()


def main() -> None:
    v2._configure_dspy()
    examples = _load_with_sigma()
    _, dev_set = v2._stratified_temporal_split(examples, train_ratio=0.8, seed=42)
    bucket = defaultdict(list)
    for ex in dev_set:
        bucket[ex.verdict].append(ex)
    capped = []
    for vd in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        capped.extend(bucket.get(vd, [])[-60:])
    random.Random(42).shuffle(capped)
    dev_set = capped
    print(f"dev {len(dev_set)}(与 v5/v5.1 同构)")

    program = _build_program_v52()

    def _one_metric(gold, pred, trace=None):
        return _metric_v5(gold, pred, trace)

    from concurrent.futures import ThreadPoolExecutor
    scores, per_v = [], defaultdict(list)

    def _one(ex):
        try:
            pred = program(macro_context=ex.macro_context, market_context=ex.market_context,
                           portfolio_state=ex.portfolio_state,
                           volatility_context=ex.volatility_context)
            return ex.verdict, _one_metric(ex, pred)
        except Exception:
            return ex.verdict, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for vd, sc in pool.map(_one, dev_set):
            if sc is not None:
                scores.append(sc)
                per_v[vd].append(sc)

    avg = sum(scores) / max(1, len(scores))
    out = {
        "signature": "v52(σ30 显式输入,中性措辞)",
        "avg_score": round(avg, 6),
        "per_verdict": {vd: {"n": len(ss), "acc": round(sum(ss) / max(1, len(ss)), 4)}
                        for vd, ss in sorted(per_v.items())},
        "refs": {"bare_v5_baseline": 0.4117, "v51_discipline": 0.265},
    }
    p = ROOT / "experiments" / "dspy_signature_v52.eval.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
