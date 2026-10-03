"""Build today's (UTC) daily strength models before the first decision that needs them.

The observed daily model is keyed by UTC day x competition x model identity x epoch and
can only be built once that day has begun. The daily refresh builds it at ~11:30 UTC, so
South American evening fixtures that cross 00:00 UTC met the new day with no model: the
first decision after midnight paid the first-build penalty, and inside a five-minute
match-window cycle the milestone expired before the retry (2026-10-03, Sao Paulo–Santos
in-play T60). The 15-minute trigger runs this; it builds only what is missing, for
competitions with a fixture to play or in progress within the next 24 hours.

    python -m prediction_market_soccer.ops.daily_model_prewarm
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prediction_market_soccer.ops.live_refresh import _LIVE_STATUS


def due_competitions(conn, now=None) -> list[str]:
    from prediction_market_soccer.config.leagues import active
    now = now or datetime.now(timezone.utc)
    statuses = ("NS", *_LIVE_STATUS)
    marks = ",".join("?" * len(statuses))
    leagues = {r[0] for r in conn.execute(
        f"SELECT DISTINCT league_id FROM fixture WHERE status_short IN ({marks}) "
        "AND kickoff_ts BETWEEN ? AND ?",
        (*statuses, (now - timedelta(hours=8)).isoformat(), (now + timedelta(hours=24)).isoformat()))}
    return [comp.key for comp in active() if comp.api_football_id in leagues]


def missing(conn, now=None) -> list[str]:
    """Due competitions whose model for today's UTC day under the active epoch is absent."""
    from prediction_market_soccer.model.observed_strength import _model_identity
    from prediction_market_soccer.util import source_history as history
    from prediction_market_soccer.util.forward_methods import active_epoch
    now = now or datetime.now(timezone.utc)
    method = active_epoch(conn)
    out = []
    for comp in due_competitions(conn, now):
        key = {'day': now.date().isoformat(), 'comp': comp, 'model_identity': _model_identity(),
               'forward_epoch_id': method['epoch_id'] if method else None}
        if not history._read_versions(conn, 'derived:daily_strength', entity_key=key):
            out.append(comp)
    return out


def prewarm(conn, comps) -> dict:
    from prediction_market_soccer.model.observed_strength import (
        build_observed_strength, ObservedInputsUnavailable)
    out = {}
    for comp in comps:
        for attempt in (1, 2):     # the first build of the UTC day always costs one round
            try:
                model = build_observed_strength(conn, datetime.now(timezone.utc).isoformat(), comp)
                connection = getattr(model, "observed_connection", None)
                if connection is not None:
                    connection.close()
                out[comp] = f"ok(attempt {attempt})"
                break
            except ObservedInputsUnavailable as exc:
                out[comp] = f"unavailable: {str(exc)[:60]}"
            except Exception as exc:  # noqa: BLE001 — one competition must not stop the rest
                out[comp] = f"{type(exc).__name__}: {str(exc)[:60]}"
                break
    return out


def main() -> None:
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    need = missing(conn)
    if not need:
        print("[daily_model_prewarm] today's models present")
        return
    print(f"[daily_model_prewarm] built {prewarm(conn, need)}")


if __name__ == "__main__":
    main()
