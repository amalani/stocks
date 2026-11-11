#!/bin/sh
# Single Task Scheduler entry point. Schedule this ONE script every 30 minutes (DSM start 06:00 -> ticks at :00/:30), every day; it decides whether to run.
#   Runs ./deploy.sh --run only Mon-Fri, every 30 min from 06:35 Pacific (06:35, 07:05, ... 13:05, 13:35), nothing after
#   13:45. Each DSM tick (:00/:30) waits DELAY seconds before running, so runs start 5 min after the open/half hour.
#   Other ticks (weekends, outside the window) log a "skip" line with the time and exit 0. The slot check also makes it safe if the task
#   is ever set to tick more often (e.g. every 5 min).
#   Pacific time is computed here, so the NAS clock/timezone does not matter and DST is handled.
#   Skips if the previous run is still going (mkdir lock), so slow runs never stack.
set -u

START=390   # 06:30 in minutes after midnight
END=825     # 13:45
STEP=30
DELAY=300   # seconds to wait after the :00/:30 tick (so the run starts at :05/:35)

cd "$(dirname "$0")"

STAMP='+%Y-%m-%d %H:%M:%S %Z'
log() { echo "run-scheduled: $(TZ=America/Los_Angeles date "$STAMP") $*"; }

DOW=$(TZ=America/Los_Angeles date +%u)   # 1=Mon .. 7=Sun
H=$(TZ=America/Los_Angeles date +%H); H=${H#0}
M=$(TZ=America/Los_Angeles date +%M); M=${M#0}
NOW=$(( ${H:-0} * 60 + ${M:-0} ))

[ "$DOW" -le 5 ] || { log "skip (weekend)"; exit 0; }
[ "$NOW" -ge "$START" ] && [ "$NOW" -le "$END" ] || { log "skip (outside window)"; exit 0; }
# only act in the first 5 minutes of each 30-min step (one run per slot even with 5-min ticks)
[ $(( (NOW - START) % STEP )) -lt 5 ] || { log "skip (not a slot)"; exit 0; }

LOCK=/tmp/screener-run.lock
if ! mkdir "$LOCK" 2>/dev/null; then
  # stale lock (> 60 min old, e.g. after a crash/reboot): clear it and carry on; otherwise a run is in progress
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
    rmdir "$LOCK" && mkdir "$LOCK" || { log "skip (lock busy)"; exit 0; }
  else
    log "skip (previous run still in progress)"
    exit 0
  fi
fi
trap 'rmdir "$LOCK"' EXIT INT TERM

sleep "$DELAY"
T0=$(date +%s)
log "start"
./deploy.sh --run
RC=$?
log "end ($(( $(date +%s) - T0 ))s, exit $RC)"
exit "$RC"
