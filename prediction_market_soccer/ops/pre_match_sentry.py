"""Pre-match sentry: a 15-minute guard for the four conditions that let a match
into the book (identity, venue listing, priceable model, PRE staging).

Invoked from refresh_and_deploy.sh's --trigger tick (every 15 min, non-pinned),
so no new scheduler is needed. Every section fails open: one broken guard must
never block the trigger path. ``--check`` reports only (no re-pull, no spawn,
no artifact write) — safe against production at any time.

Sections
  calendar     same-day kickoff moves: force-refresh fixtures for any competition
               with a kickoff inside the next 4h (TTL bypass; the 2026-09-10 UCL
               pair missed PRE because the stored kickoff_ts was stale). At most
               one re-pull per competition per 30 min.
  pre_watchdog ALERT when a fixture passed kickoff without a PRE observation;
               WARN when one is due inside 12 min and still unstaged; WARN when
               the last-24h minimum staging lead drops under 12 min (starvation
               canary — the 2026-09-11 CPU contention gave a 9.4-min lead).
  pit_cache    rebuild data/output/pit_records.json in the background when it is
               older than the 36h staleness rule (once per 6h at most).
  disk         free-space floor: WARN under 10 GB, ALERT under 2 GB (the
               2026-09-11 disk-full killed live cycles for ~50 min).
  poly_global  reference prices from Polymarket GLOBAL for upcoming matches that
               Polymarket US does not list (reason event_not_found_in_complete_
               catalog). Display/monitoring artifact ONLY — never a certified
               quote, never a ledger input; the certified fallback rides a
               forward-method epoch (see the coverage-audit notes).
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
LEAGUES = (39, 140, 135, 78, 61, 2, 3, 848, 13, 11, 71, 128)
OBSERVATION_CUTOVER = "2026-09-10T18:18:02+00:00"   # paper_observation table start
ARTIFACT = MOD / "data" / "output" / "pre_match_sentry.json"
GLOBAL_REF = MOD / "data" / "output" / "poly_global_reference.json"
STAMPS = MOD / "data" / "runtime"


def _now():
    return datetime.now(timezone.utc)


def _stamp_ok(name: str, minutes: int) -> bool:
    p = STAMPS / f"sentry_{name}.stamp"
    if p.exists() and time.time() - p.stat().st_mtime < minutes * 60:
        return False
    return True


def _touch(name: str) -> None:
    STAMPS.mkdir(parents=True, exist_ok=True)
    (STAMPS / f"sentry_{name}.stamp").write_text(_now().isoformat())


def calendar_recheck(conn, *, check: bool) -> dict:
    from prediction_market_soccer.config.leagues import active
    now = _now()
    horizon = (now + timedelta(hours=4)).isoformat()
    rows = conn.execute(
        "SELECT DISTINCT league_id FROM fixture WHERE status_short='NS' AND kickoff_ts BETWEEN ? AND ?",
        (now.isoformat(), horizon)).fetchall()
    due = {r[0] for r in rows}
    comps = [c for c in active() if c.api_football_id in due]
    refreshed = []
    for comp in comps:
        if not _stamp_ok(f"cal_{comp.key}", 30):
            continue
        if check:
            refreshed.append(f"{comp.key} (would refresh)")
            continue
        from prediction_market_soccer.ingest.api_football import ApiFootball
        from prediction_market_soccer.ingest.soccer_ingest import sync_fixtures
        sync_fixtures(ApiFootball(conn), conn, comp, force=True)
        _touch(f"cal_{comp.key}")
        refreshed.append(comp.key)
    return {"due_comps": sorted(c.key for c in comps), "refreshed": refreshed}


def pre_watchdog(conn) -> dict:
    now = _now()
    lid = ",".join(str(x) for x in LEAGUES)
    pre = ("EXISTS(SELECT 1 FROM paper_observation o WHERE o.fixture_api_id=f.api_id "
           "AND json_extract(o.payload,'$.milestone')='PRE')")
    missed = [r[0] for r in conn.execute(
        f"SELECT f.api_id FROM fixture f WHERE f.league_id IN ({lid}) "
        f"AND f.kickoff_ts BETWEEN ? AND ? AND f.kickoff_ts > ? AND NOT {pre}",
        ((now - timedelta(hours=6)).isoformat(), now.isoformat(), OBSERVATION_CUTOVER)).fetchall()]
    due = [r[0] for r in conn.execute(
        f"SELECT f.api_id FROM fixture f WHERE f.league_id IN ({lid}) AND f.status_short='NS' "
        f"AND f.kickoff_ts BETWEEN ? AND ? AND NOT {pre}",
        (now.isoformat(), (now + timedelta(minutes=12)).isoformat())).fetchall()]
    # Lead = the EARLIEST PRE staging per fixture (later re-observations are routine),
    # then the worst fixture across the last 24h — the starvation canary.
    lead = conn.execute(
        f"SELECT MIN(lead) FROM (SELECT MAX((julianday(f.kickoff_ts)-julianday(o.observed_at))*1440) lead "
        f"FROM fixture f JOIN paper_observation o ON o.fixture_api_id=f.api_id "
        f"AND json_extract(o.payload,'$.milestone')='PRE' "
        f"WHERE f.league_id IN ({lid}) AND f.kickoff_ts BETWEEN ? AND ? GROUP BY f.api_id)",
        ((now - timedelta(hours=24)).isoformat(), now.isoformat())).fetchone()[0]
    state = "ALERT" if missed else ("WARN" if due or (lead is not None and lead < 12) else "ok")
    return {"state": state, "kickoff_passed_unstaged": missed, "due_unstaged": due,
            "min_lead_min_24h": round(lead, 1) if lead is not None else None}


def pit_cache(*, check: bool) -> dict:
    p = MOD / "data" / "output" / "pit_records.json"
    age_h = (time.time() - p.stat().st_mtime) / 3600 if p.exists() else None
    stale = age_h is None or age_h > 36
    spawned = False
    if stale and not check and _stamp_ok("pit_rebuild", 360):
        import subprocess
        subprocess.Popen([sys.executable, "-m", "prediction_market_soccer.exec.kalshi_mirror",
                          "--build-pit-cache"],
                         stdout=open(MOD / "data" / "logs" / "pit_cache_sentry.log", "a"),
                         stderr=sys.modules["subprocess"].STDOUT, start_new_session=True)
        _touch("pit_rebuild")
        spawned = True
    return {"age_hours": round(age_h, 1) if age_h is not None else None,
            "stale": stale, "rebuild_spawned": spawned}


def disk() -> dict:
    """Free space plus the fill RATE, because the floor alone warns far too late.

    A single epoch activation writes a ~12.5 GB pre-change DB snapshot, and the three of
    2026-09-13 consumed 38 GB overnight; the 2026-09-11 outage began the same way and the
    10 GB floor would not have fired until an in-flight paper entry had already been lost.
    """
    free = shutil.disk_usage(MOD / "data").free
    state = "ALERT" if free < 2 * 2**30 else ("WARN" if free < 10 * 2**30 else "ok")
    out = {"state": state, "free_gb": round(free / 2**30, 1)}
    mark = STAMPS / "disk_free.json"
    try:
        prior = json.loads(mark.read_text())
        hours = (_now() - datetime.fromisoformat(prior["at"])).total_seconds() / 3600
        if hours >= 0.5:
            rate = (prior["free_gb"] - out["free_gb"]) / hours
            out["gb_per_hour"] = round(rate, 2)
            # Anything above this empties a healthy 150 GB margin inside a day.
            if rate > 5 and out["state"] == "ok":
                out["state"] = "WARN"
                out["reason"] = f"free space falling {rate:.1f} GB/h"
            mark.write_text(json.dumps({"at": _now().isoformat(), "free_gb": out["free_gb"]}))
    except (OSError, ValueError, KeyError):
        pass
    if not mark.exists():
        STAMPS.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps({"at": _now().isoformat(), "free_gb": out["free_gb"]}))
    return out


def poly_global_reference(*, check: bool) -> dict:
    """Reference-only Global prices for matches Poly-US does not list."""
    up = MOD / "data" / "output" / "upcoming.json"
    if not up.exists():
        return {"state": "skip", "reason": "no upcoming.json"}
    doc = json.loads(up.read_text())
    now = _now()
    targets = []
    for m in doc.get("matches", []):
        qs = ((m.get("quote_status_details") or {}).get("poly_us") or {})
        if qs.get("reason") != "event_not_found_in_complete_catalog":
            continue
        ko = m.get("kickoff")
        if not ko:
            continue
        try:
            dt = datetime.fromisoformat(str(ko).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now <= dt <= now + timedelta(hours=48):
            targets.append(m)
    if not targets:
        return {"state": "ok", "candidates": 0}
    if check or not _stamp_ok("poly_global", 30):
        return {"state": "skip", "candidates": len(targets), "reason": "check-mode or stamp"}
    from prediction_market_soccer.venues.polymarket_global.reader import (
        PolymarketGlobalReader, poly_club_id)
    reader = PolymarketGlobalReader()
    d0 = now.date().isoformat()
    d1 = (now + timedelta(days=3)).date().isoformat()
    events = reader.list_match_events(end_date_min=d0, end_date_max=d1, max_requests=12)
    by_key = {}
    for ev in events:
        ids = frozenset(poly_club_id(t) for t in ev.get("teams", {}) if not t.startswith("Draw")) - {""}
        if len(ids) == 2:
            by_key[(ids, ev.get("date"))] = ev
    rows, matched = [], 0
    for m in targets:
        hid, aid = poly_club_id(str(m.get("home") or "")), poly_club_id(str(m.get("away") or ""))
        ev = by_key.get((frozenset({hid, aid}) - {""}, m.get("et_date"))) if hid and aid else None
        if ev is None:
            rows.append({"fixture_id": m.get("fixture_id"), "state": "no_global_event"})
            continue
        prices = {}
        for label, token in ev.get("teams", {}).items():
            try:
                book = reader.get_book(token)
                bid = getattr(book, "yes_bid", None)
                ask = getattr(book, "yes_ask", None)
                side = "draw" if label.startswith("Draw") else (
                    "home" if poly_club_id(label) == hid else "away")
                prices[side] = {"bid": float(bid) if bid is not None else None,
                                "ask": float(ask) if ask is not None else None}
            except Exception as exc:  # noqa: BLE001 — one token must not sink the pass
                prices[label] = {"error": type(exc).__name__}
        matched += 1
        rows.append({"fixture_id": m.get("fixture_id"), "slug": ev.get("slug"),
                     "state": "ok", "prices": prices})
    _touch("poly_global")
    from prediction_market_soccer.ops.run_status import atomic_json
    atomic_json(GLOBAL_REF, {
        "as_of": now.isoformat(), "provider": "poly_global", "tier": "reference_only",
        "note": "Display/monitoring only. Never a certified quote or ledger input; "
                "the certified fallback requires a forward-method epoch change.",
        "candidates": len(targets), "matched": matched, "rows": rows})
    return {"state": "ok", "candidates": len(targets), "matched": matched}


def main() -> None:
    check = "--check" in sys.argv[1:]
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    out, t0 = {}, time.time()
    for name, fn in (("calendar", lambda: calendar_recheck(conn, check=check)),
                     ("pre_watchdog", lambda: pre_watchdog(conn)),
                     ("pit_cache", lambda: pit_cache(check=check)),
                     ("disk", disk),
                     ("poly_global", lambda: poly_global_reference(check=check))):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 — fail open, always
            out[name] = {"state": "error", "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    out["as_of"] = _now().isoformat()
    out["elapsed_s"] = round(time.time() - t0, 1)
    if not check:
        from prediction_market_soccer.ops.run_status import atomic_json
        atomic_json(ARTIFACT, out)
    states = {k: v.get("state", "ok") if isinstance(v, dict) else "ok" for k, v in out.items()
              if k not in ("as_of", "elapsed_s")}
    print(f"[pre_match_sentry] {'CHECK ' if check else ''}"
          + " ".join(f"{k}={v}" for k, v in states.items())
          + f" ({out['elapsed_s']}s)")
    if any(v == "ALERT" for v in states.values()):
        print(f"[pre_match_sentry] ALERT detail: {json.dumps(out, ensure_ascii=False)[:400]}")


if __name__ == "__main__":
    main()
