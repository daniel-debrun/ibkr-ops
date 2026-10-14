#!/usr/bin/env bash
# cron-run.sh <timeout-seconds> <command...>
#
# Shell wrapper for scheduled jobs. Complements ibkr_ops.cron.run (lock, pre-flight, schedule window,
# heartbeat, alert) with a hard timeout and kill, a minimal environment, and a UTC timestamp on each output
# line, appended to one log file per job per UTC day.
#
#   50 15 * * 1-5  $HOME/ibkr-ops/scripts/cron-run.sh 2400 python3 -m myjobs.daily
#
# Debian/Ubuntu cron ignores CRON_TZ and schedules in the host's local time. Use local times in the crontab
# together with a schedule window (cron.run(window=...)), or use the systemd timers in deploy/systemd.
set -euo pipefail

limit=${1:?usage: cron-run.sh <timeout-seconds> <command...>}
shift
[[ $limit =~ ^[0-9]+$ ]] || { echo "timeout must be whole seconds" >&2; exit 64; }
[[ $# -gt 0 ]] || { echo "no command" >&2; exit 64; }

state=${IBKR_OPS_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ibkr-ops}
mkdir -p "$state/logs"
name=$(basename -- "$1")
for a in "$@"; do case $a in -m) next=1 ;; *) [[ ${next:-} == 1 ]] && { name=$a; next=; } ;; esac; done
name=${name//[^A-Za-z0-9._-]/_}
log="$state/logs/$name-$(date -u +%Y%m%d).log"

export PATH=/usr/local/bin:/usr/bin:/bin:${PATH:-}
export PYTHONUNBUFFERED=1

set +e
timeout -k 30 "$limit" "$@" 2>&1 | while IFS= read -r line; do
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line"
done >>"$log"
rc=${PIPESTATUS[0]}
set -e
[[ $rc -eq 124 || $rc -eq 137 ]] && printf '%s cron-run: killed after %ss timeout\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$limit" >>"$log"
exit "$rc"
