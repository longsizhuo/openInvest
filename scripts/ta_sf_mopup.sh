#!/bin/bash
# SF 补漏循环 v2：bracketed pgrep（防匹配自身/包装进程的命令行文本）
cd /home/ubuntu/invest-ta
EXP=memory/.ta_experiment
while pgrep -f "[t]a_phase_a\.py .*--analysts" > /dev/null; do sleep 30; done
for pass in 1 2 3 4 5 6; do
  echo "[mopup] pass $pass $(date -u +%H:%M)" >> $EXP/phase_a_sf.log
  uv run python -u scripts/ta_phase_a.py --analysts sentiment,fundamental --workers 3 >> $EXP/phase_a_sf.log 2>&1
  if tail -6 $EXP/phase_a_sf.log | grep -q "待跑 0"; then break; fi
  sleep 90
done
echo "SF_DONE" >> $EXP/phase_a_sf.log
