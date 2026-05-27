# archive_few_shot_failed/

DSPy few-shot 路线验证失败的 offline 实验产物归档（**留作证据，勿删勿接 production**）。

这里是 v4（用 Phase 1.5 数据 + path_c oracle 重训的尝试）的 trainset / stats：

| 文件 | 说明 |
|------|------|
| `dspy_trainset_v4_base.json` | Phase 1.5 窗口（NDQ.AX+GC=F，2023-07~2024-06，每 4 交易日 × 5 portfolio_state）的 v2 格式 trainset，605 样本 |
| `dspy_trainset_v4.json` | 上面经 path_c oracle 重标后的 v4 trainset |
| `dspy_trainset_v4.stats.json` | v4 verdict 分布：BUY 59.5% / HOLD 20% / SELL 20.5%，**ACCUMULATE=0% / TRIM=0%**（塌缩比 v3 的 52% BUY 更重）|
| `dspy_trainset_v4_base.stats.json` | base trainset 统计 |

**为什么归档而不接入**：见 [`docs/wiki/adr/007-few-shot-retirement.md`](../../docs/wiki/adr/007-few-shot-retirement.md)。
一句话：path_c oracle 结构上只能产出 BUY/HOLD/SELL 三档，ACCUMULATE/TRIM 数学上不可能胜出，换数据救不了；v1-v4 四版 few-shot 在 holdout 上均跑输 zero-shot。**v4 没跑 holdout**——塌缩检查已从机理锁定结论，没必要复证。
