# Sweep Ground Truth 事件清单

纯算术 sweep（不需要 LLM）的 ground truth 事件清单。用于验证 regime 分类阈值在已知历史事件上的表现。

## 添加规则

1. **不能修改已有文件** — sweep 运行后 `regime_events.yaml` 是只读的（git timestamp 强制校验）
2. **新事件用新文件** — 如 `regime_events_v2.yaml`，需要重新 commit
3. **事件必须是公开市场事件** — 不能用只有你自己知道的信号
4. **日期范围必须有数据支撑** — yfinance 能拉到该日期范围内的行情

## 文件格式

```yaml
metadata:
  version: 1
  created: "2026-05-27"
  description: "..."
  lock_note: "..."

events:
  - name: "事件名称"
    start: "YYYY-MM-DD"
    end: "YYYY-MM-DD"
    expected_regime: "crash"  # crash/downtrend/recovery/uptrend/range_bound
    severity: "high"          # extreme/high/moderate/low
    notes: "人话备注"
```

## 使用

```bash
uv run python -m scripts.sweep_runner \
  --mode arithmetic \
  --param regime.trend_ma_spread_pct \
  --range 2.0,8.0,0.5 \
  --train-start 2018-01-01 \
  --train-end 2023-12-31 \
  --assets NDQ.AX,GC=F \
  --ground-truth docs/wiki/sweep_ground_truth/regime_events.yaml
```
