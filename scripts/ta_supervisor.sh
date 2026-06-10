#!/bin/bash
# Phase A 全自动管线：GDELT 抓完 → 清 smoke → 全量跑批 → eval → DONE 标记
cd /home/ubuntu/invest-ta
EXP=memory/.ta_experiment
TARGET=244

# 1. 循环抓新闻直到缓存齐（fetch 脚本单轮会跳过失败 key，多轮收敛）
for round in 1 2 3 4 5 6 7 8 9 10; do
  n=$(wc -l < $EXP/news_cache.jsonl 2>/dev/null || echo 0)
  if [ "$n" -ge "$TARGET" ]; then break; fi
  echo "[supervisor] fetch round $round (cache $n/$TARGET)" >> $EXP/supervisor.log
  if ! pgrep -f "ta_data.py fetch_news" > /dev/null; then
    uv run python -u scripts/ta_data.py fetch_news >> $EXP/fetch_news.log 2>&1
  else
    sleep 120
  fi
done
n=$(wc -l < $EXP/news_cache.jsonl 2>/dev/null || echo 0)
echo "[supervisor] fetch 结束 cache=$n" >> $EXP/supervisor.log
if [ "$n" -lt "$TARGET" ]; then
  echo "[supervisor] FETCH_INCOMPLETE" > $EXP/STATUS; exit 1
fi

# 2. 清 smoke 行（smoke 时 news 缓存为空，报告被污染）重跑全量
rm -f $EXP/phase_a_reports.jsonl
echo "[supervisor] phase A 开跑 $(date -u +%H:%M)" >> $EXP/supervisor.log
uv run python -u scripts/ta_phase_a.py --workers 6 >> $EXP/phase_a.log 2>&1

# 3. eval
uv run python scripts/eval_ta_signal.py > $EXP/eval_result.md 2>&1
echo "DONE $(date -u +%FT%H:%M)" > $EXP/STATUS
echo "[supervisor] 全部完成" >> $EXP/supervisor.log
