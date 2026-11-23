#!/usr/bin/env bash
# Run the M6 k6 scripts on winpc, the load generator (outside the lab cluster, so the load does not compete for the
# CPU it measures). Long runs go through a scheduled task (M6K6, SYSTEM) so they survive an ssh disconnect and a
# sleeping Mac; short ones (seed, the oversell rush) can run in the foreground.
#
#   winpc.sh start <script.js> [-e K=V …]   copy scripts/m6/k6 to winpc, start k6 in the task; prints the run's name
#   winpc.sh fg    <script.js> [-e K=V …]   the same in the foreground (output streamed; for runs of a few minutes)
#   winpc.sh status                          the task's state and the end of the current run's log
#   winpc.sh wait                            block until the task is no longer running
#   winpc.sh fetch [dir]                     copy the results back (default scripts/m6/out/k6): <run>.summary.json,
#                                            <run>.csv.gz (start runs), <run>.log
#   winpc.sh stop                            stop the task and k6
#
# Every run gets -e CATALOG_URL/ORDER_URL/INVENTORY_URL from $M6_URLS (default scripts/m6/out/urls.env, written by
# expose.py), -e SUMMARY=<run>.summary.json, -e STARTED=<epoch>; start runs also --out csv=<run>.csv.gz.
# Needs on winpc: F:\k6\k6.exe (v2.4.0). Files live in F:\m6 (k6\, results\). Scripts stay ASCII: PowerShell reads
# copied UTF-8 files as GBK.
set -euo pipefail
HOST=${M6_HOST:-winpc}
HERE=$(cd "$(dirname "$0")" && pwd)
URLS=${M6_URLS:-$HERE/out/urls.env}
K6=${M6_K6:-F:\\k6\\k6.exe}

ps() {  # a PowerShell snippet on winpc (base64 -EncodedCommand: no quoting through ssh and cmd)
  local enc
  enc=$(printf '%s' "$1" | iconv -t UTF-16LE | base64 | tr -d '\n')
  ssh -o ConnectTimeout=8 "$HOST" "powershell -NoProfile -EncodedCommand $enc" 2>/dev/null | LC_ALL=C tr -d '\000\r' | grep -av 'CLIXML\|<Objs\|</Objs' || true
}

job() {  # job <mode> <script> [-e K=V …]: write the k6 command line as F:\m6\job.ps1; print the run name
  local mode=$1 script=$2; shift 2
  [ -f "$HERE/k6/$script" ] || { echo "no $HERE/k6/$script" >&2; exit 1; }
  [ -f "$URLS" ] || { echo "no $URLS (run expose.py up first, or set M6_URLS)" >&2; exit 1; }
  local run args=() line
  run="$(date +%Y%m%d-%H%M%S)-${script%.js}"
  while IFS= read -r line; do
    [[ $line =~ ^[A-Z_]+=.+$ ]] && args+=("-e" "$line")
  done < "$URLS"
  args+=("$@" "-e" "SUMMARY=F:\\m6\\results\\$run.summary.json" "-e" "STARTED=$(date +%s)")
  [ "$mode" = start ] && args+=("--out" "csv=F:\\m6\\results\\$run.csv.gz")
  local ps1="\$ErrorActionPreference = 'Continue'
Set-Location F:\\m6\\k6
\$log = 'F:\\m6\\results\\$run.log'
'$run' | Out-File -Encoding ascii F:\\m6\\current.txt
& '$K6' run --no-color"
  for a in "${args[@]}"; do
    [[ $a == *"'"* ]] && { echo "argument with a single quote not supported: $a" >&2; exit 1; }
    ps1+=" '$a'"
  done
  ps1+=" '$script' *>> \$log
\"exit \$LASTEXITCODE\" | Out-File -Append -Encoding ascii \$log"
  LC_ALL=C grep -q '[^ -~]' <<<"$ps1" && { echo "non-ASCII in the job (PowerShell would misread it)" >&2; exit 1; }
  local tmp; tmp=$(mktemp -d)
  printf '%s\r\n' "$ps1" > "$tmp/job.ps1"
  ps 'New-Item -ItemType Directory -Force F:\m6\k6, F:\m6\results | Out-Null; Remove-Item F:\m6\k6\* -Force -ErrorAction SilentlyContinue' >/dev/null
  scp -q "$HERE"/k6/*.js "$HOST:F:/m6/k6/"
  scp -q "$tmp/job.ps1" "$HOST:F:/m6/job.ps1"
  rm -rf "$tmp"
  echo "$run"
}

cmd=${1:-}; shift || true
case "$cmd" in
  start)
    [ $# -ge 1 ] || { echo "usage: winpc.sh start <script.js> [-e K=V …]" >&2; exit 1; }
    state=$(ps '(Get-ScheduledTask -TaskName M6K6 -ErrorAction SilentlyContinue).State')
    [ "$state" = "Running" ] && { echo "a run is already going (winpc.sh status / stop)" >&2; exit 1; }
    run=$(job start "$@")
    ps '$a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File F:\m6\job.ps1"
$p = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName M6K6 -Action $a -Principal $p -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName M6K6; Start-Sleep 2
"task: " + (Get-ScheduledTask -TaskName M6K6).State' >&2
    echo "$run"
    ;;
  fg)
    [ $# -ge 1 ] || { echo "usage: winpc.sh fg <script.js> [-e K=V …]" >&2; exit 1; }
    run=$(job fg "$@")
    echo "run: $run" >&2
    ps 'powershell -NoProfile -ExecutionPolicy Bypass -File F:\m6\job.ps1; Get-Content (Join-Path F:\m6\results ((Get-Content F:\m6\current.txt) + ".log"))'
    ;;
  status)
    ps '"task: " + (Get-ScheduledTask -TaskName M6K6 -ErrorAction SilentlyContinue).State
$c = Get-Content F:\m6\current.txt -ErrorAction SilentlyContinue
if ($c) { "run: $c"; Get-Content "F:\m6\results\$c.log" -Tail 12 -ErrorAction SilentlyContinue }'
    ;;
  wait)
    while [ "$(ps '(Get-ScheduledTask -TaskName M6K6 -ErrorAction SilentlyContinue).State')" = "Running" ]; do sleep 15; done
    echo "M6K6 not running"
    ;;
  fetch)
    dir=${1:-$HERE/out/k6}; mkdir -p "$dir"
    scp -q "$HOST:F:/m6/results/*" "$dir/" && ls -1t "$dir" | head -8
    ;;
  stop)
    ps 'Stop-ScheduledTask -TaskName M6K6 -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "k6.exe" -and $_.CommandLine -like "*F:\m6\*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "stopped k6 " + $_.ProcessId }
# the command line of a SYSTEM process is not always readable: when the capacity runner is not running either, every
# k6 left is ours (2026-11-23: one survived the filter above and loaded the lab for 10 more minutes)
if ((Get-ScheduledTask -TaskName LoadTest -ErrorAction SilentlyContinue).State -ne "Running") {
  Get-Process k6 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.Id -Force; "stopped k6 " + $_.Id }
}'
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
