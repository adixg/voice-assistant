#!/usr/bin/env bash
# Kills training if Windows' committed memory passes LIMIT_GB (WSL gets shut
# down near the 42 GB commit limit). Logs readings to commit_guard.log.
cd "$(dirname "$0")"
LIMIT=${1:-38}
while pgrep -f "train_rina.py" >/dev/null || [ ! -f .guard_started ]; do
  touch .guard_started
  GB=$(/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command '$os=Get-CimInstance Win32_OperatingSystem; [math]::Round(($os.TotalVirtualMemorySize-$os.FreeVirtualMemory)/1MB,1)' 2>/dev/null | tr -d '\r')
  echo "$(date +%T) commit ${GB} GB" >> commit_guard.log
  if [ -n "$GB" ] && awk -v g="$GB" -v l="$LIMIT" 'BEGIN{exit !(g>l)}'; then
    echo "$(date +%T) KILL: commit ${GB} GB > ${LIMIT} GB" >> commit_guard.log
    pkill -KILL -f "model_train_eval"; pkill -KILL -f "train_rina.py"; break
  fi
  sleep 10
done
rm -f .guard_started
