#!/bin/bash
# 收尾管线：补完最后的新闻缓存 → news 批次（多轮补漏）→ 终版 eval → STATUS
cd /home/ubuntu/invest-ta
EXP=memory/.ta_experiment
TARGET=244

for round in $(seq 1 12); do
  n=$(wc -l < $EXP/news_cache.jsonl 2>/dev/null || echo 0)
  if [ "$n" -ge "$TARGET" ]; then break; fi
  echo "[finisher] fetch round $round (cache $n/$TARGET)" >> $EXP/supervisor.log
  uv run python -u scripts/ta_data.py fetch_news >> $EXP/fetch_news.log 2>&1
  sleep 30
done
n=$(wc -l < $EXP/news_cache.jsonl 2>/dev/null || echo 0)
if [ "$n" -lt "$TARGET" ]; then echo "FETCH_INCOMPLETE cache=$n" > $EXP/STATUS; exit 1; fi

# news 批次 + 补漏（断点续跑，绝不 rm）
for pass in $(seq 1 6); do
  echo "[finisher] phase A pass $pass $(date -u +%H:%M)" >> $EXP/supervisor.log
  uv run python -u scripts/ta_phase_a.py --workers 3 >> $EXP/phase_a.log 2>&1
  if tail -6 $EXP/phase_a.log | grep -q "待跑 0"; then break; fi
  sleep 90
done

uv run python scripts/eval_ta_signal.py > $EXP/eval_result.md 2>&1
echo "DONE $(date -u +%FT%H:%M)" > $EXP/STATUS
echo "[finisher] 全部完成" >> $EXP/supervisor.log
