#!/usr/bin/env bash
# The storefront v2 perf env, driven from the Mac: system under test on winpc (WSL distro `ci`, Docker Compose project
# sf2perf in /opt/sf2perf), load generator on xiaoxin (k6 binary + knee.py in ~/sf2perf, plain processes: nothing in
# the lab's Kubernetes). See docs/m6-verification.md "Independent scaling on winpc".
#
#   perf.sh sync                         copy compose/config to winpc and k6/knee.py to xiaoxin (+ k6 binary if missing)
#   perf.sh up [order=N] [inventory=N] [sync=on|off]
#                                        start / rescale with the CPU sizes of perf.env (default 1 replica each;
#                                        sync=off: Postgres synchronous_commit=off, the diagnostic run only)
#   perf.sh ps | down                    status / stop everything (volumes stay: `down -v` only by hand)
#   perf.sh seed                         products + stock for the flash sale (seed.js, idempotent; tops up the stock)
#   perf.sh knee <label> [knee.py args]  run the knee search on xiaoxin in the background (nohup), log in ~/sf2perf/
#   perf.sh log <label> | fetch          follow a run / copy ~/sf2perf/results to ./results
#   perf.sh firewall on|off              the Hyper-V firewall rule admitting xiaoxin's tailnet address to the perf ports
#   perf.sh wsl '<bash>'                 run a command as root in the ci distro
#
# xiaoxin and winpc are not on the same LAN (different sites): the load goes over Tailscale (direct path). Nothing on
# the path is a Kubernetes NodePort (the 11-23 lab failure mode); judged on server-side metrics (knee.py).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WINPC=${WINPC:-winpc}
XIAOXIN_SSH=(ssh -o ServerAliveInterval=30 ${XIAOXIN_SSH_OPTS:--o HostName=100.76.236.114 -o HostKeyAlias=192.168.3.205} xiaoxin)
WINPC_ADDR=${WINPC_ADDR:-100.106.140.35}
LOADGEN_ADDR=${LOADGEN_ADDR:-100.76.236.114}
K6_VERSION=${K6_VERSION:-v2.4.0}
DIR=/opt/sf2perf

wsl() {   # bash script → root in the ci distro (base64: no quoting through ssh / cmd.exe / wsl.exe)
  local b; b=$(printf '%s' "$1" | base64 | tr -d '\n')
  ssh "$WINPC" "wsl.exe -d ci -u root -- bash -c \"echo $b | base64 -d | bash\""
}
ps1() {   # PowerShell script (ASCII) → winpc
  local b; b=$(printf '%s' "$1" | iconv -t UTF-16LE | base64 | tr -d '\n')
  ssh "$WINPC" "powershell -NoProfile -EncodedCommand $b" 2>&1 | grep -av -E 'CLIXML|<Obj|progress|schemas.micro' || true
}
xx() { "${XIAOXIN_SSH[@]}" "$@"; }

cmd=${1:-}; shift || true
case "$cmd" in
  sync)
    (cd "$HERE" && COPYFILE_DISABLE=1 tar czf - compose.yaml haproxy.cfg prometheus.yml rabbitmq.conf cgroup_exporter.py perf.env) \
      | base64 | ssh "$WINPC" "wsl.exe -d ci -u root -- bash -c \"mkdir -p $DIR && base64 -d | tar xzf - -C $DIR 2>/dev/null; echo winpc: synced\""
    (COPYFILE_DISABLE=1 tar czf - -C "$REPO/scripts/m6/k6" lib.js seed.js -C "$HERE/k6" flash.js -C "$HERE" knee.py) \
      | xx "mkdir -p ~/sf2perf/k6 ~/sf2perf/bin && tar xzf - -C ~/sf2perf 2>/dev/null; mv -f ~/sf2perf/lib.js ~/sf2perf/seed.js ~/sf2perf/flash.js ~/sf2perf/k6/; \
            [ -x ~/sf2perf/bin/k6 ] || (cd /tmp && curl -fsSL https://github.com/grafana/k6/releases/download/$K6_VERSION/k6-$K6_VERSION-linux-amd64.tar.gz | tar xz && mv k6-$K6_VERSION-linux-amd64/k6 ~/sf2perf/bin/ && rm -rf k6-$K6_VERSION-linux-amd64); \
            ~/sf2perf/bin/k6 version | head -1; echo xiaoxin: synced"
    ;;
  up)
    order=1 inventory=1 sync=on
    for kv in "$@"; do case "$kv" in order=*) order=${kv#order=};; inventory=*) inventory=${kv#inventory=};; sync=*) sync=${kv#sync=};;
      *) echo "up: $kv?" >&2; exit 2;; esac; done
    wsl "cd $DIR && cp perf.env .env && echo PG_SYNC_COMMIT=$sync >> .env && docker compose up -d --wait --scale order=$order --scale inventory=$inventory 2>&1 | grep -v -E 'Running|Healthy|Waiting|Exited|Started|Starting|Created|Recreate|Pulled' || true; \
         docker compose ps --format '{{.Service}} {{.Status}}' | sort | uniq -c"
    ;;
  ps)   wsl "cd $DIR && docker compose ps --format 'table {{.Name}}\t{{.Status}}' && docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'" ;;
  down) wsl "cd $DIR && docker compose down 2>&1 | tail -3; docker ps --format '{{.Names}}' | grep -c sf2perf || true" ;;
  seed)
    xx "cd ~/sf2perf/k6 && ../bin/k6 run -q -e CATALOG_URL=http://$WINPC_ADDR:18081 -e INVENTORY_URL=http://$WINPC_ADDR:18082 -e STOCK=${STOCK:-3000000} seed.js 2>&1 | grep -E 'checks|seeded|ERRO' || true"
    ;;
  knee)
    label=${1:?label}; shift
    xx "cd ~/sf2perf && ulimit -n \$(ulimit -Hn) && setsid -f nohup python3 -u knee.py --label $label --prom http://$WINPC_ADDR:19090 --order-url http://$WINPC_ADDR:18080 $* > knee-$label.log 2>&1 < /dev/null; echo started knee $label: ~/sf2perf/knee-$label.log"
    ;;
  log)  xx "tail -n ${N:-40} ~/sf2perf/knee-${1:?label}.log" ;;
  fetch) mkdir -p "$HERE/results"; xx "cd ~/sf2perf && tar czf - results knee-*.log" | tar xzf - -C "$HERE/results" && ls "$HERE/results" ;;
  firewall)
    case "${1:-}" in
      on)  ps1 "\$vm='{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'; Remove-NetFirewallHyperVRule -Name sf2perf-loadgen -ErrorAction SilentlyContinue; New-NetFirewallHyperVRule -Name sf2perf-loadgen -DisplayName 'storefront v2 perf env (from xiaoxin only)' -Direction Inbound -VMCreatorId \$vm -Protocol TCP -LocalPorts 18080,18081,18082,19090 -RemoteAddresses $LOADGEN_ADDR -Action Allow | Out-Null; Get-NetFirewallHyperVRule -Name sf2perf-loadgen | Format-List Name,LocalPorts,RemoteAddresses,Enabled" ;;
      off) ps1 "Remove-NetFirewallHyperVRule -Name sf2perf-loadgen -ErrorAction SilentlyContinue; 'removed'" ;;
      *) echo "firewall on|off" >&2; exit 2 ;;
    esac ;;
  wsl) wsl "$1" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
