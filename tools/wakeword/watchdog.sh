#!/usr/bin/env bash
# Kills the training job if its resident memory passes LIMIT_MB, so a memory
# blow-up stops training instead of taking WSL (and everything in it) down.
# Logs each new peak to watchdog.log. Usage: watchdog.sh [LIMIT_MB]
cd "$(dirname "$0")"
LIMIT=${1:-5000}; PEAK=0; SEEN=0
while true; do
  KB=0; for p in $(pgrep -f "model_train_eval|train_rina.py"); do a=$(awk "/RssAnon/{print \$2}" /proc/$p/status 2>/dev/null); KB=$((KB + ${a:-0})); done
  if [ "$KB" -eq 0 ]; then
    [ $SEEN -eq 1 ] && { echo "$(date +%T) job ended, peak ${PEAK} MB" >> watchdog.log; exit 0; }
    sleep 1; continue
  fi
  SEEN=1; MB=$((KB / 1024))
  [ $MB -gt $PEAK ] && { PEAK=$MB; echo "$(date +%T) peak ${PEAK} MB" >> watchdog.log; }
  if [ $MB -gt "$LIMIT" ]; then
    echo "$(date +%T) KILL: ${MB} MB > ${LIMIT} MB" >> watchdog.log
    pkill -KILL -f "model_train_eval"; pkill -KILL -f "train_rina.py"; exit 1
  fi
  sleep 1
done
