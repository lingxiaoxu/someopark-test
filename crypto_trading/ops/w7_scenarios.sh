#!/bin/bash
# W7 scenario x entry-timing tables: rolling 14 days, rebuilt every 4 hours.
# Read-only for trading: never touches W7/W8/W9/W10 services or order paths.
#
#   crypto_trading/ops/w7_scenarios.sh run       # build -> check -> (retry once from raw) -> promote
#   crypto_trading/ops/w7_scenarios.sh install   # launchd agent, 01/05/09/13/17/21:25 local (every 4h)
#   crypto_trading/ops/w7_scenarios.sh status|stop|latest
#
# Output: crypto_trading/trading_signals/w7_scenarios/
#   latest/tables.md|tables.json                         the 5 coin tables + pooled table
#   latest/derived.md|derived.json                       best entry time per coin x scenario (same run)
#   latest/meta.json|check.json                          last run that PASSED the checks
#   history/<stamp>.md|.json                            every promoted run (30 days)
#   table_vs_t8/latest.md|ledger.csv                    table-driven prod vs T-8-only, refreshed each run
#   research/sizing_weekly_<date>.md                    sizing/timing mathematics, once a week
#   status.json                                         outcome of the most recent attempt
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${W7S_PYTHON:-/Users/xuling/miniforge3/envs/someopark_run/bin/python}"
OUT="$REPO/crypto_trading/trading_signals/w7_scenarios"
LABEL="com.someopark.crypto.w7scenarios"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$REPO/crypto_trading/logs/w7_scenarios.log"
MOD="crypto_trading.crypto_strategies.w7_scenarios"
LOCK="$OUT/.lock"
export PYTHONPATH="$REPO" PYTHONUNBUFFERED=1

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') [w7_scenarios] $*" >&2; }   # stderr: stdout carries the run dir

notify() {  # best effort desktop notice on failure
  /usr/bin/osascript -e "display notification \"$1\" with title \"W7 情形表\"" >/dev/null 2>&1 || true
}

build_and_check() {  # $@ extra build args; prints run dir on success
  local run
  run="$(nice -n 15 "$PY" -m "$MOD.run" build --workers 3 "$@" 2>>"$LOG" | tail -1)"
  if [ -z "$run" ] || [ ! -f "$run/meta.json" ]; then
    log "build failed ($*)"; return 1
  fi
  if nice -n 15 "$PY" -m "$MOD.check" "$run" >>"$LOG" 2>&1; then
    echo "$run"; return 0
  fi
  log "check FAILED for $run: $("$PY" -c 'import json,sys;print("; ".join(json.load(open(sys.argv[1]))["errors"])[:600])' "$run/check.json" 2>/dev/null)"
  return 1
}

promote() {  # $1 run dir, $2 attempts
  local run="$1" stamp tmp
  stamp="$(basename "$run")"
  tmp="$OUT/.latest.$$"
  mkdir -p "$tmp" "$OUT/history"
  cp "$run/tables.md" "$run/tables.json" "$run/derived.md" "$run/derived.json" "$run/meta.json" "$run/check.json" "$tmp/"
  rm -rf "$OUT/.latest.old"
  [ -d "$OUT/latest" ] && mv "$OUT/latest" "$OUT/.latest.old"
  mv "$tmp" "$OUT/latest" && rm -rf "$OUT/.latest.old"
  cp "$run/tables.md" "$OUT/history/$stamp.md"
  cp "$run/tables.json" "$OUT/history/$stamp.json"
  cp "$run/derived.md" "$OUT/history/$stamp.derived.md"
  cp "$run/derived.json" "$OUT/history/$stamp.derived.json"
  "$PY" - "$OUT/status.json" "$stamp" "$2" <<'PYS'
import json, sys, time
json.dump({"ok": True, "run": sys.argv[2], "attempts": int(sys.argv[3]), "at": time.time(),
           "at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PYS
  log "promoted $stamp (attempts=$2)"
  # Table-vs-T-8 live ledger (2026-10-02): read-only, never blocks the tables.
  (cd "$REPO" && "$PY" -m crypto_trading.crypto_strategies.w7_scenarios.live_vs_t8 >/dev/null 2>&1) \
    || log "table_vs_t8 report failed (non-fatal)"
  # Weekly sizing/timing mathematics (user 2026-10-02): research only, once per 7 days.
  local stampf="$OUT/research/.weekly_stamp" last=0
  [ -f "$stampf" ] && last="$(cut -d. -f1 "$stampf")"
  if [ $(( $(date +%s) - last )) -ge $(( 7 * 86400 )) ]; then
    (cd "$REPO" && nice -n 15 "$PY" -m crypto_trading.crypto_strategies.w7_scenarios.weekly_math >/dev/null 2>&1) \
      && log "weekly sizing report written" || log "weekly sizing report failed (non-fatal)"
  fi
}

prune() {
  find "$OUT/runs" -mindepth 1 -maxdepth 1 -type d -mtime +2 -exec rm -rf {} + 2>/dev/null
  find "$OUT/history" -type f -mtime +30 -delete 2>/dev/null
  find "$OUT/cache" -type f -mtime +30 -delete 2>/dev/null
}

refresh_macro_calendar() {  # the W7 prod macro-release guard's forward schedule (FRED + Fed page); non-fatal, loud when stale
  local out rc
  out="$(cd "$REPO" && "$PY" -m crypto_trading.crypto_common.macro_calendar refresh 2>&1 | tail -1)"; rc=$?
  log "macro calendar refresh rc=$rc ${out:0:300}"
  if [ "$rc" -ne 0 ]; then
    if "$PY" -m crypto_trading.crypto_common.macro_calendar status 2>/dev/null | grep -q '"stale": true'; then
      notify "宏观发布日历刷新失败且已过期(>7 天):实盘宏观降仓守卫将不生效(fail open)。见 logs/w7_scenarios.log"
    fi
  fi
}

only_stale_errors() {  # $1 run dir: true when every check error is a data-freshness one
  "$PY" - "$1/check.json" <<'PYS' 2>/dev/null
import json, sys
e = json.load(open(sys.argv[1])).get("errors", [])
sys.exit(0 if e and all(x.startswith("STALE:") for x in e) else 1)
PYS
}

do_run() {
  mkdir -p "$OUT" "$(dirname "$LOG")"
  if ! mkdir "$LOCK" 2>/dev/null; then
    local pid age
    pid="$(cat "$LOCK/pid" 2>/dev/null)"
    age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || echo 0) ))
    if [ -n "$pid" ] && [ "$age" -lt 7200 ] && ps -p "$pid" -o command= 2>/dev/null | grep -q "w7_scenarios.sh"; then
      log "another run ($pid, ${age}s) in progress; skip"
      "$PY" -c 'import json,sys,time;p=sys.argv[1];d=json.load(open(p)) if __import__("os").path.exists(p) else {};d.update(skipped_at=time.time());json.dump(d,open(p,"w"),ensure_ascii=False,indent=1)' "$OUT/status.json"
      return 0
    fi
    log "taking over stale lock (pid=${pid:-?}, age=${age}s)"
    rm -rf "$LOCK"; mkdir "$LOCK"
  fi
  echo $$ > "$LOCK/pid"
  trap "rm -rf '$LOCK'" EXIT
  log "start"
  refresh_macro_calendar
  local run last
  if run="$(build_and_check)"; then
    promote "$run" 1
  else
    last="$(ls -1d "$OUT"/runs/* 2>/dev/null | tail -1)"
    if [ -n "$last" ] && only_stale_errors "$last"; then
      log "only data-freshness errors: waiting 120s, then one more plain build"
      sleep 120; run="$(build_and_check)"; local rc=$?
    else
      log "retrying once from raw recorder files (cache cleared)"
      run="$(build_and_check --no-cache)"; local rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
      promote "$run" 2
    else
      "$PY" - "$OUT/status.json" <<'PYS'
import glob, json, os, sys, time
runs = sorted(glob.glob(os.path.join(os.path.dirname(sys.argv[1]), "runs", "*")))
errs = []
if runs and os.path.exists(os.path.join(runs[-1], "check.json")):
    errs = json.load(open(os.path.join(runs[-1], "check.json"))).get("errors", [])
json.dump({"ok": False, "attempts": 2, "at": time.time(), "at_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
           "errors": errs or ["build failed — see crypto_trading/logs/w7_scenarios.log"],
           "note": "latest/ still holds the previous run that passed its checks"},
          open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PYS
      log "FAILED twice; latest/ unchanged"
      notify "两次生成都没通过检查，latest 保留上一版。见 logs/w7_scenarios.log"
      prune; return 1
    fi
  fi
  prune
  return 0
}

case "${1:-status}" in
  run) do_run >>"$LOG" 2>&1 ;;
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
    W7S_REPO="$REPO" W7S_PLIST="$PLIST" W7S_LOG="$LOG" "$PY" - <<'PYS'
import os, plistlib
from pathlib import Path
r = os.environ["W7S_REPO"]
p = dict(Label="com.someopark.crypto.w7scenarios",
         ProgramArguments=["/bin/bash", r + "/crypto_trading/ops/w7_scenarios.sh", "run"],
         WorkingDirectory=r, RunAtLoad=False, Nice=15,
         # every 4 hours at :25 local (US Eastern) — clear of W7 entries (:07 :22 :37 :52)
         # and of Kalshi's Thursday 03:00-05:00 ET maintenance (no 15M tape then)
         StartCalendarInterval=[{"Hour": h, "Minute": 25} for h in (1, 5, 9, 13, 17, 21)],
         StandardOutPath=os.environ["W7S_LOG"], StandardErrorPath=os.environ["W7S_LOG"],
         EnvironmentVariables=dict(PYTHONUNBUFFERED="1"))
Path(os.environ["W7S_PLIST"]).write_bytes(plistlib.dumps(p))
PYS
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      launchctl bootout "gui/$(id -u)/$LABEL"
    fi
    launchctl bootstrap "gui/$(id -u)" "$PLIST" && echo "installed $LABEL (01/05/09/13/17/21:25 local)"
    ;;
  status) launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|last exit|runs" ; cat "$OUT/status.json" 2>/dev/null ;;
  latest) cat "$OUT/latest/tables.md" "$OUT/latest/derived.md" ;;
  stop) launchctl bootout "gui/$(id -u)/$LABEL" ;;
  *) echo "usage: $0 run|install|status|latest|stop"; exit 2 ;;
esac
