#!/usr/bin/env bash
# Phase 1.5: 365 天 leak-free 回测驱动（2023-07-01 → 2024-06-30）
# 8 个日期分片并行跑 backtest_committee（每个进程独立 patch cutoff，安全）。
# 全部 ≤ 2024-06-30，自动满足 LLM_TRAINING_CUTOFF 护栏，无需 --allow-lookahead。
set -u
cd "$(dirname "$0")/.."

ASSETS="NDQ.AX,GC=F"
LOGDIR="/tmp/phase15_logs"
mkdir -p "$LOGDIR"
export INVEST_MAX_DEBATE_ROUNDS=1   # 1 轮辩论，提速

# 8 个 ~46 天的分片
SHARDS=(
  "2023-07-01:2023-08-15"
  "2023-08-16:2023-09-30"
  "2023-10-01:2023-11-15"
  "2023-11-16:2023-12-31"
  "2024-01-01:2024-02-15"
  "2024-02-16:2024-03-31"
  "2024-04-01:2024-05-15"
  "2024-05-16:2024-06-30"
)

echo "=== Phase 1.5 启动 $(date) ：${#SHARDS[@]} 个分片并行 ==="
pids=()
for s in "${SHARDS[@]}"; do
  start="${s%%:*}"; end="${s##*:}"
  log="$LOGDIR/shard_${start}_${end}.log"
  uv run --no-sync python -m scripts.backtest_committee \
    --start "$start" --end "$end" --assets "$ASSETS" > "$log" 2>&1 &
  pids+=($!)
  echo "  分片 $start → $end  pid=$! log=$log"
done

echo "=== 等待 ${#pids[@]} 个分片完成 ==="
fail=0
for pid in "${pids[@]}"; do
  wait "$pid" || fail=$((fail+1))
done
echo "=== 全部分片结束 $(date) ，失败 $fail 个 ==="

# 统计写出的 backtest 交易日数
n=$(ls -d memory/.backtest/2023-* memory/.backtest/2024-* 2>/dev/null | wc -l)
echo "=== memory/.backtest/ 现有 leak-free 交易日目录: $n ==="
