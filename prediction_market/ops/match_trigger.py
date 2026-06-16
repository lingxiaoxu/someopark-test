"""ops/match_trigger.py — fire the refresh pipeline right after each match finishes.

Run every ~15 minutes by launchd. It is a cheap GATE:
  * outside the match window (no fixture kicked off in the last ~3h) → SKIP, no API
    call, no work — so on quiet hours/days it costs nothing;
  * inside the window → pull finished results (one API call) and compare the settled
    count to a stored watermark. If a NEW match has finished → print "RUN" (the shell
    then does the full refresh + build + deploy); otherwise → "SKIP".

This makes the pipeline event-driven: a day with three matches triggers three runs,
each taking the newest result into the (growing) OOS sample. Prints a single
RUN/SKIP line; the wrapper greps for it (robust to conda-run exit-code quirks).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prediction_market.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


def decide(conn=None) -> str:
    from prediction_market.ingest import store
    conn = conn or store.init_db()
    now = datetime.now(timezone.utc)

    # Match window: any fixture that kicked off in the last 3h (≈ still finishing)
    # or about to start in the next 15 min. No API call to evaluate this.
    lo = (now - timedelta(hours=3)).isoformat()
    hi = (now + timedelta(minutes=15)).isoformat()
    in_window = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE kickoff_ts BETWEEN ? AND ?", (lo, hi)).fetchone()["n"]
    if not in_window:
        return "SKIP: outside match window (no API call)"

    # Inside the window — pull the latest finished results (cheap, incremental).
    try:
        from prediction_market.ingest.api_football import ApiFootball
        from prediction_market.ingest.soccer_ingest import sync_results
        sync_results(ApiFootball(conn), conn)
    except Exception as e:
        return f"SKIP: result ingest failed ({e})"

    settled = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(
            ",".join("?" * len(_FINISHED))), _FINISHED).fetchone()["n"]

    CONFIG.paths.ensure()
    wm = CONFIG.paths.output / ".trigger_watermark"
    prev = int(wm.read_text()) if wm.exists() else -1
    wm.write_text(str(settled))
    if prev < 0:
        return f"SKIP: baseline set (settled={settled})"   # first run after install — don't fire
    if settled > prev:
        return f"RUN: new result(s) — settled {prev} -> {settled}"
    return f"SKIP: no new result (settled={settled})"


if __name__ == "__main__":
    print(decide())
