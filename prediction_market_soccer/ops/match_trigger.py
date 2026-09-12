"""Trigger on unacknowledged results; acknowledge only after a successful publish."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ops.run_status import atomic_bytes, read_status
from prediction_market_soccer.ops.maintenance_gate import writer


def _settled(conn):
    return conn.execute("SELECT COUNT(*) FROM fixture WHERE status_short IN ('FT','AET','PEN') "
                        "AND home_goals IS NOT NULL").fetchone()[0]


def _previous():
    try:
        return int((CONFIG.paths.output / ".trigger_watermark").read_text().strip())
    except (OSError, ValueError):
        return -1


@writer
def acknowledge(value: int) -> None:
    if value < 0:
        raise ValueError("invalid result watermark")
    atomic_bytes(CONFIG.paths.output / ".trigger_watermark", str(value).encode())


def decide(conn=None) -> str:
    from prediction_market_soccer.ingest import store
    own = conn is None
    conn = conn or store.init_db()
    try:
        prev = _previous()
        # Retry pending results even after the match window ends. A failed prior run
        # cannot consume the event or leave reports stuck until tomorrow.
        settled = _settled(conn)
        if settled > prev:
            return f"RUN: unacknowledged results {prev} -> {settled}"
        now = datetime.now(timezone.utc)
        from prediction_market_soccer.config.leagues import active
        lids = tuple(c.api_football_id for c in active())
        if not lids:
            return "SKIP: no active competitions"
        in_window = conn.execute(
            f"SELECT COUNT(*) FROM fixture WHERE league_id IN ({','.join('?' for _ in lids)}) "
            "AND kickoff_ts BETWEEN ? AND ?", (*lids, (now - timedelta(hours=3)).isoformat(),
                                                (now + timedelta(minutes=15)).isoformat())).fetchone()[0]
        if not in_window:
            return "SKIP: outside match window (no API call)"
        from prediction_market_soccer.ingest.api_football import ApiFootball
        from prediction_market_soccer.ingest.soccer_ingest import sync_results
        # Failure propagates: a network error is not a normal idle cycle.
        sync_results(ApiFootball(conn), conn)
        settled = _settled(conn)
        return f"RUN: new results {prev} -> {settled}" if settled > prev else f"SKIP: no new result ({settled})"
    finally:
        if own:
            conn.close()


@writer
def acknowledge_refresh(status=None) -> int:
    """Consume only the current attempt's successfully promoted input count.

    last_success_input describes a fully healthy run and may remain older when
    optional history collection is incomplete. It is never a fallback here.
    """
    status = read_status("full_refresh") if status is None else status
    published = status.get("published_input")
    steps = status.get("steps")
    if (status.get("name") != "full_refresh" or status.get("state") not in ("ok", "degraded")
            or not isinstance(published, dict) or published.get("schema_version") != 1
            or published.get("attempt_at") != status.get("last_attempt_at")
            or not isinstance(steps, list) or not steps
            or not any(s.get("required") is True for s in steps)
            or any(s.get("state") not in ("ok", "partial", "failed") for s in steps)
            or any(s.get("required") is True and s.get("state") != "ok" for s in steps)):
        raise RuntimeError("No completed required refresh publication to acknowledge")
    try:
        times = [datetime.fromisoformat(str(value).replace("Z", "+00:00")) for value in
                 (status.get("last_attempt_at"), published.get("published_at"), status.get("finished_at"))]
        if any(t.tzinfo is None for t in times) or not times[0] <= times[1] <= times[2]:
            raise ValueError("invalid publication clock")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid refresh publication timestamps") from exc
    value = published.get("settled_results")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("Invalid published result count")
    acknowledge(value)
    return value


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--acknowledge-refresh", action="store_true")
    args = ap.parse_args()
    if args.acknowledge_refresh:
        acknowledge_refresh()
    else:
        print(decide())


if __name__ == "__main__":
    main()
