#!/bin/bash
# v2：fetch 收敛 → phase A 补缺（断点续跑，绝不 rm）→ eval → STATUS
cd /home/ubuntu/invest-ta
EXP=memory/.ta_experiment
TARGET=244

for round in $(seq 1 40); do
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
if [ "$n" -lt "$TARGET" ]; then echo "FETCH_INCOMPLETE cache=$n" > $EXP/STATUS; exit 1; fi

# 等其它 phase A 进程退出（防并发双写）
while pgrep -f "ta_phase_a.py" > /dev/null; do sleep 60; done
echo "[supervisor] phase A 补缺 $(date -u +%H:%M)" >> $EXP/supervisor.log
uv run python -u scripts/ta_phase_a.py --workers 6 >> $EXP/phase_a.log 2>&1

uv run python scripts/eval_ta_signal.py > $EXP/eval_result.md 2>&1
echo "DONE $(date -u +%FT%H:%M)" > $EXP/STATUS
echo "[supervisor] 全部完成" >> $EXP/supervisor.log
