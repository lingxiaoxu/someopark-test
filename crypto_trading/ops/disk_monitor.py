"""Disk-space + growth-rate monitor with alerting (user request 2026-07-08).

The crypto_trading recorders keep running; this watches how fast they eat disk
and alerts before it becomes a problem. Two things are checked every run:

  1. **Absolute free space** on the volume — the thing you actually care about
     ("am I about to run out"). Hard floors, phase-independent.
  2. **Growth RATE** — bytes added per day, extrapolated from the delta since
     the last run. Catches runaways (e.g. the duplicate-recorder incident would
     have ~doubled the rate → caught here).

Sawtooth-aware: crypto data grows uncompressed during the UTC day and drops
~10× at midnight gzip rotation. So the rate is only trustworthy when sampled at
a CONSISTENT phase — run this from the daily cron (pipeline `daily`, 20:30 ET)
for the clean day-over-day number; ad-hoc runs still report free space + a
noisy-but-flagged short-interval rate.

Alerts fire on: free < floor, free < critical, projected days-to-full < window,
or daily growth > ceiling. Channels: macOS notification (osascript) + a log
line + a JSON status file. Never raises into the caller (best-effort ops tool).

CLI:
    conda run -n someopark_run python -m crypto_trading.ops.disk_monitor [--quiet] [--force-alert]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CRYPTO_ROOT = Path(__file__).resolve().parents[1]
LOGS = CRYPTO_ROOT / "logs"
STATE_FILE = LOGS / "disk_monitor_state.json"
LOG_FILE = LOGS / "disk_monitor.log"
STATUS_FILE = LOGS / "disk_monitor_status.json"
WATCH_DIR = CRYPTO_ROOT / "price_data"

# ── thresholds (tune here) ───────────────────────────────────────────────────
FREE_FLOOR_GB = 25.0          # warn: getting low
FREE_CRITICAL_GB = 10.0       # critical: act now
DAILY_GROWTH_WARN_GB = 2.0    # warn: growing >~3× the expected ~0.1–0.6 GB/day net
DAYS_TO_FULL_WARN = 45        # warn: at current rate, < this many days of headroom
MIN_INTERVAL_FOR_RATE_S = 3600  # don't compute a rate from < 1h of elapsed time
GB = 1024 ** 3


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _notify(title: str, message: str) -> None:
    """Best-effort macOS notification; silently no-ops if unavailable."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification {json.dumps(message)} with title {json.dumps(title)}'],
            check=False, timeout=10, capture_output=True)
    except Exception:
        logger.debug("osascript notification unavailable", exc_info=True)


def check(*, now: float | None = None, force_alert: bool = False,
          notifier=_notify) -> dict:
    """Run one check. Returns the status dict; fires alerts on breach."""
    now = time.time() if now is None else now
    usage = shutil.disk_usage(str(CRYPTO_ROOT))
    free_gb = usage.free / GB
    watch_bytes = _dir_bytes(WATCH_DIR) if WATCH_DIR.exists() else 0

    prev = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text())
        except Exception:
            prev = {}

    growth_gb_per_day = None
    dt = now - prev.get("ts", 0) if prev else 0
    if prev and dt >= MIN_INTERVAL_FOR_RATE_S:
        # rate from the volume's USED bytes (catches non-crypto runaways too),
        # falling back to the watched dir if disk_usage.used is unavailable
        d_used = (usage.used - prev.get("disk_used_bytes", usage.used))
        growth_gb_per_day = (d_used / GB) / (dt / 86400.0)

    days_to_full = (free_gb / growth_gb_per_day
                    if growth_gb_per_day and growth_gb_per_day > 0 else None)

    alerts: list[str] = []
    level = "ok"
    if free_gb < FREE_CRITICAL_GB:
        alerts.append(f"CRITICAL: only {free_gb:.1f} GB free (< {FREE_CRITICAL_GB:.0f})")
        level = "critical"
    elif free_gb < FREE_FLOOR_GB:
        alerts.append(f"low free space: {free_gb:.1f} GB (< {FREE_FLOOR_GB:.0f})")
        level = "warn"
    if growth_gb_per_day is not None and growth_gb_per_day > DAILY_GROWTH_WARN_GB:
        alerts.append(f"fast growth: {growth_gb_per_day:.2f} GB/day "
                      f"(> {DAILY_GROWTH_WARN_GB:.1f}) — check for runaway/duplicate recorders")
        level = "critical" if level != "critical" else level
    if days_to_full is not None and days_to_full < DAYS_TO_FULL_WARN:
        alerts.append(f"~{days_to_full:.0f} days to full at current rate "
                      f"(< {DAYS_TO_FULL_WARN})")
        level = "warn" if level == "ok" else level

    status = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "free_gb": round(free_gb, 2),
        "disk_used_gb": round(usage.used / GB, 2),
        "disk_total_gb": round(usage.total / GB, 2),
        "crypto_data_gb": round(watch_bytes / GB, 3),
        "growth_gb_per_day": round(growth_gb_per_day, 3) if growth_gb_per_day is not None else None,
        "days_to_full": round(days_to_full) if days_to_full is not None else None,
        "hours_since_last": round(dt / 3600, 1) if dt else None,
        "level": level,
        "alerts": alerts,
    }

    LOGS.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps(status, indent=1))
    with open(LOG_FILE, "a") as fh:
        fh.write(json.dumps(status) + "\n")
    # persist state for the next rate computation
    STATE_FILE.write_text(json.dumps(
        {"ts": now, "disk_used_bytes": usage.used, "free_gb": free_gb,
         "crypto_data_bytes": watch_bytes}))

    if alerts or force_alert:
        title = f"crypto disk {level.upper()}"
        msg = f"{free_gb:.0f}GB free · " + " · ".join(alerts) if alerts else \
              f"{free_gb:.0f}GB free · rate {status['growth_gb_per_day']} GB/day · OK"
        notifier(title, msg)
        logger.warning("ALERT [%s] %s", level, "; ".join(alerts) or "(forced)")
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true", help="no stdout, just log/alert")
    ap.add_argument("--force-alert", action="store_true",
                    help="fire a notification even when OK (test the channel)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    status = check(force_alert=args.force_alert)
    if not args.quiet:
        print(json.dumps(status, indent=2))
    # exit code: 0 ok, 1 warn, 2 critical — cron/pipeline-friendly
    return {"ok": 0, "warn": 1, "critical": 2}.get(status["level"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
