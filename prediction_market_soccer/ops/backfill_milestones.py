"""Bounded historical reference collection into an explicit candidate target.

Never rewrites milestone_snapshot/price_tick/financial records. FT is a separately
observed result, not a synthetic kickoff+95 quote. Existing FT rows do not suppress
missing-side collection; fixed scopes and collection task state control retries.
"""
from __future__ import annotations

import json
import inspect
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.util.match_timeline import milestone_target, score_at_clock, APPROXIMATE_HALFTIME_MIN as _HALFTIME_WALL_MIN
from prediction_market_soccer.util.price_history import series_receipt, sample_price
from prediction_market_soccer.util.research_inputs import epoch, digest

ET = ZoneInfo("America/New_York")
_FINISHED = ("FT", "AET", "PEN")
_MILESTONES = [("PRE", -5, -5), ("T15", 15, 15), ("T30", 30, 30),
               ("HT", 45, 47), ("T60", 60, 75), ("T75", 75, 90)]
_MAX_BAR_GAP_S = 180
_SERIES_PRE_S, _SERIES_POST_S = 1800, 10200

def _load_aliases(comp_key: str) -> dict[str, str]:
    """{venue spelling -> club_id} for one competition (bootstrap + curated, §3.6)."""
    try:
        doc = json.loads(
            (CONFIG.paths.priors / f"aliases_{comp_key}.json").read_text(encoding="utf-8"))
        return dict(doc.get("aliases") or {})
    except Exception:
        return {}


class _ClubResolver:
    """Outcome title → reviewed immutable identity; collisions remain unmapped.

    Competition membership can disambiguate a registered name. Gamma sometimes
    tags UEFA ties under a domestic prefix, so an absent in-comp name may use a
    globally unique identity. No substring, token deletion or fuzzy fallback.
    """

    def __init__(self, conn):
        from prediction_market_soccer.util.club_identity import venue_identity_index
        from prediction_market_soccer.venues.polymarket_us.discovery import _load_alias_pairs
        records = [dict(r) for r in conn.execute(
            "SELECT club_id,comp,api_team_id,name FROM club_registry")]
        self._club_ids = {r['club_id'] for r in records}
        aliases = _load_alias_pairs()
        self._global = venue_identity_index(allowed_ids=self._club_ids, records=records, aliases=aliases)
        self._scoped = {comp: venue_identity_index(
            allowed_ids={r['club_id'] for r in records if r['comp'] == comp},
            records=records, aliases=aliases) for comp in {r['comp'] for r in records}}
        self.unmapped: list[str] = []   # deduped; surfaced in the run summary

    def resolve(self, label: str, comp_key: str | None) -> str | None:
        s = (label or "").strip()
        if not s:
            return None
        scoped = self._scoped.get(comp_key)
        index = scoped if scoped and scoped.candidates(s) else self._global
        cid = index.resolve(s)
        if cid:
            return cid
        if s not in self.unmapped:
            self.unmapped.append(s)
        return None


def _shift_day(iso_date: str, days: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


def _score_at(conn, fixture_id, hi_api, ai_api, minute, *, extra=0, period=None,
              event_revision=None):
    """Explicit reference adapter. Never silently read the final event projection."""
    if event_revision is None:
        raise ValueError("an explicit complete event revision is required")
    if not event_revision.get("complete"):
        raise ValueError("partial event revision")
    state = score_at_clock(event_revision["events"], hi_api, ai_api,
                           elapsed=minute, extra=extra, period=period)
    return state["home_goals"], state["away_goals"]


_UEFA_LADDER = {
    "knockout round play-offs": "ro16", "knockout round play-off": "ro16",
    "round of 16": "ro8",
    "quarter-finals": "ro4", "quarterfinals": "ro4", "quarter finals": "ro4",
    "semi-finals": "finalist", "semifinals": "finalist", "semi finals": "finalist",
}


def _advance_round_key(comp, round_name: str | None) -> str | None:
    """Which per-club 'reaches X' market this fixture's winner enters, or None.

    Registry-driven (TRANSFORM_PLAN §3.0): the fixture must sit on an advance-capable
    stage, and the competition must actually list the rung. Only the Swiss-format UEFA
    competitions have a league phase to qualify INTO, so `advance` is theirs alone —
    the CONMEBOL/Argentine advance markets are per-MATCH tickers on Kalshi, not the
    per-club reach series this Poly Global backfill reads, so they return None here
    and their advance columns stay NULL rather than being filled from the wrong book.
    """
    from prediction_market_soccer.config.leagues import Stage, caps_for, stage_of
    if comp is None or not round_name:
        return None
    if not caps_for(comp.key, round_name).advance:
        return None
    rn = round_name.strip().lower()
    if stage_of(comp.key, round_name) == Stage.CUP_TWO_LEG and comp.kind == "swiss_ucl":
        if rn in {"play-offs", "playoffs", "qualifying play-offs", "qualification play-offs"}:
            return "advance" if comp.kalshi.get("advance") else None
        if "qualif" in rn or "prelimin" in rn:
            return None  # winning an early tie is not qualification for league play
    rung = _UEFA_LADDER.get(rn)
    return rung if (rung and comp.kalshi.get(rung)) else None


def _validate_collection(conn, writer, scope, fixture_ids, limit):
    if conn is None or writer is None or scope is None:
        raise ValueError("historical collection requires explicit source, candidate writer and fixed scope")
    if hasattr(conn, 'conn'):
        conn = conn.conn
    if writer.conn is conn:
        raise ValueError("candidate writer must be separate from source connection")
    src = conn.execute("PRAGMA database_list").fetchone()[2]
    if src:
        from pathlib import Path
        if Path(src).resolve() == writer.path or (writer.path.exists() and Path(src).samefile(writer.path)):
            raise ValueError("candidate cannot alias source")
    ids = tuple(scope.fixture_ids)
    if fixture_ids is not None and tuple(fixture_ids) != ids:
        raise ValueError("fixture_ids must exactly match fixed scope")
    if limit is not None and limit < len(ids):
        raise ValueError("limit must be applied before scope is frozen")
    if writer.scope_id != scope.scope_id or not set(ids).issubset(writer.fixture_ids):
        raise ValueError("candidate scope mismatch")
    return conn, ids


def _receipt(reader, token, ko, fidelity=1):
    start, end = ko - _SERIES_PRE_S, ko + _SERIES_POST_S
    begun = datetime.now(timezone.utc).isoformat()
    if hasattr(reader, "prices_history_receipt"):
        r = reader.prices_history_receipt(token, fidelity=fidelity, start_ts=start, end_ts=end)
        return series_receipt(r["points"], identity={"token_id": token, "provider": "poly_global"},
                              start_ts=start, end_ts=end, fidelity=fidelity,
                              request_started_at=r["request_started_at"], received_at=r["received_at"],
                              raw=r.get("raw"), raw_hash=r.get("raw_hash"), provider_request_window=r.get("request_window"))
    points = reader.prices_history(token, fidelity=fidelity, start_ts=start, end_ts=end)
    return series_receipt(points, identity={"token_id": token, "provider": "poly_global"},
                          start_ts=start, end_ts=end, fidelity=fidelity, request_started_at=begun,
                          received_at=datetime.now(timezone.utc).isoformat())


def _rescheduled_binding(ev, fx, sides, hi, ai, resolver):
    """Prove an unchanged regulation contract survived a rescheduled fixture.

    Slug/question dates remain original provider identifiers. A later endDate or
    matching opponents alone is not schedule evidence. Only the actual aware
    event start and all three selected contracts' game starts can bridge dates.
    """
    from prediction_market_soccer.util.market_identity import yes_token

    def aware(value):
        if not isinstance(value, str):
            raise ValueError("missing explicit start time")
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None or dt.utcoffset() is None:
            raise ValueError("start time must include timezone")
        return dt.astimezone(timezone.utc)

    try:
        raw, contracts = ev["raw"], ev["contracts"]
        start = aware(fx["kickoff_ts"])
        if ev.get("identity_complete") is not True or aware(raw.get("startTime")) != start:
            return None
        dates = {start.date().isoformat(), start.astimezone(ET).date().isoformat()}
        if raw.get("eventDate") and raw["eventDate"] not in dates:
            return None
        raw_teams = raw.get("teams") or []
        if len(raw_teams) != 2 or {t.get("ordering") for t in raw_teams} != {"home", "away"}:
            return None
        expected = {"home": hi, "away": ai}
        if any(resolver.resolve(t.get("name"), ev["comp"]) != expected[t["ordering"]] for t in raw_teams):
            return None
        selected, conditions = {}, set()
        for side, token in sides.items():
            found = [c for c in contracts.values() if c.get("token_id") == token]
            if len(found) != 1:
                return None
            contract = found[0]
            mk = contract["raw"]
            if contract.get("raw_hash") != digest(mk) or yes_token(mk) != token:
                return None
            mid = mk.get("conditionId") or mk.get("id")
            if not mid or mid in conditions or contract.get("market_id") != mid:
                return None
            conditions.add(mid)
            if sum(m == mk for m in (raw.get("markets") or [])) != 1:
                return None
            if aware(mk.get("gameStartTime")) != start or mk.get("sportsMarketType") != "moneyline":
                return None
            question = mk.get("question") or ""
            if side == "draw":
                q = re.fullmatch(r"Will (.+?) vs\. (.+?) end in a draw\?", question, re.I)
                if not q or [resolver.resolve(q[i], ev["comp"]) for i in (1, 2)] != [hi, ai]:
                    return None
            else:
                q = re.fullmatch(r"Will (.+?) win on (\d{4}-\d{2}-\d{2})\?", question, re.I)
                if not q or q[2] != ev["date"] or resolver.resolve(q[1], ev["comp"]) != expected[side]:
                    return None
                if resolver.resolve(mk.get("groupItemTitle"), ev["comp"]) != expected[side]:
                    return None
            description = " ".join((mk.get("description") or "").lower().split())
            postponement = "if the game is postponed, this market will remain open until the game has been completed."
            if re.findall(r"if the game is postponed[^.]*\.", description) != [postponement]:
                return None
            if "this market refers only to the outcome within the first 90 minutes of regular play plus stoppage time." not in description:
                return None
            selected[side] = {"token_id": token, "market_id": mid, "raw_hash": digest(mk),
                              "game_start_at": mk["gameStartTime"]}
        return {"basis": "explicit_rescheduled_regulation_contract", "fixture_kickoff": fx["kickoff_ts"],
                "original_slug_date": ev["date"], "event_start_at": raw["startTime"],
                "event_raw_hash": digest(raw), "contracts": selected}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _identity_for_fixture(fx, comp, cmap, events, resolver):
    hi, ai = cmap.get(fx["home_api_id"]), cmap.get(fx["away_api_id"])
    if not hi or not ai or hi == ai or comp is None:
        return None, "identity_unresolved"
    day = datetime.fromtimestamp(epoch(fx["kickoff_ts"]), timezone.utc).astimezone(ET).date().isoformat()
    days = {day, _shift_day(day, -1), _shift_day(day, 1)}
    matches = []
    for ev in events:
        if ev.get("comp") != comp.key:
            continue
        mapped = {}
        conflict = False
        for label, token in (ev.get("teams") or {}).items():
            cid = "draw" if label.casefold().startswith("draw") else resolver.resolve(label, comp.key)
            if cid is None:
                continue
            if cid in mapped:
                conflict = True
            mapped[cid] = token
        if hi not in mapped or ai not in mapped:
            continue
        if conflict or not ev.get("identity_complete", True):
            if ev.get("date") in days:
                return None, "identity_conflict"
            continue
        sides = {"home": mapped.get(hi), "draw": mapped.get("draw"), "away": mapped.get(ai)}
        if not all(sides.values()) or len(set(sides.values())) != 3:
            if ev.get("date") in days:
                return None, "identity_conflict"
            continue
        if ev.get("date") not in days:
            schedule = _rescheduled_binding(ev, fx, sides, hi, ai, resolver)
            if schedule is None:
                continue
            ev = {**ev, "schedule_binding": schedule}
        matches.append((ev, sides))
    if len(matches) != 1:
        return None, "identity_conflict" if matches else "not_listed"
    return matches[0], None


def _catalog(reader, method_name, *args, max_requests, **kwargs):
    """One bounded logical discovery, accounting for actual HTTP dispatches.

    Old injected readers with **kwargs remain usable. A reader that cannot accept
    the budget is not called: retrying on TypeError could repeat real requests.
    """
    if max_requests <= 0:
        return None, {"complete": False, "state": "not_requested", "requests": 0}, "request_budget_exhausted"
    method = getattr(reader, method_name)
    parameters = inspect.signature(method).parameters.values()
    if not any(p.name == "max_requests" or p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters):
        return None, {"complete": False, "state": "unsupported", "requests": 0}, "discovery_budget_unsupported"
    previous = getattr(reader, "discovery_status", None)
    error, data = None, None
    try:
        data = method(*args, max_requests=max_requests, **kwargs)
    except Exception as exc:
        error = "discovery_failed:" + type(exc).__name__
    current = getattr(reader, "discovery_status", None)
    status = dict(current or {"complete": error is None, "state": "legacy_reader"})
    # A failed call must not reuse the previous discovery's request count.
    if error and current is previous:
        status = {"complete": False, "state": "failed"}
    attempts = status.get("requests", status.get("pages", 1))
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= max_requests:
        return None, {**status, "complete": False, "requests": max_requests}, "invalid_discovery_request_count"
    status["requests"] = attempts
    return data, status, error


def collect(conn=None, *, scope=None, fixture_ids=None, writer=None, reader=None,
            state_conn=None, limit=None, market_kind="match", ticks=False, fidelity=1, timeline_provider=None):
    """Common positive path for milestone/advance/tick collectors.

    Returns per-target outcomes. Writer has already durably saved every referenced
    observation before completion state is recorded. Caller seals the combined run.
    """
    conn, ids = _validate_collection(conn, writer, scope, fixture_ids, limit)
    collector = "ticks" if ticks else "milestones"
    targets = tuple(t for t in scope.target_ids if t.endswith(":" + market_kind) and (t.startswith("tick:") == ticks))
    if not targets:
        targets = tuple(f"{m}:{s}:{market_kind}" for m in (["tick"] if ticks else [x[0] for x in _MILESTONES])
                        for s in (["home", "draw", "away"] if market_kind == "match" else ["home", "away"]))
    result = {"fixtures": len(ids), "matched": 0, "rows": 0, "items": [], "complete": True,
              "status": "complete", "discovery": None, "requests": 0}
    if not ids or market_kind not in scope.market_kinds:
        return result
    due = {(fid, t) for fid in ids for t in targets}
    if state_conn is not None:
        from prediction_market_soccer.util.collection_state import due_tasks
        due &= {(r["fixture_id"], r["target_id"]) for r in due_tasks(state_conn, scope, collector)}
    if not due:
        return result
    fixtures = {r["api_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM fixture WHERE api_id IN (" + ",".join("?" for _ in ids) + ")", ids)}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute("SELECT api_id,canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    from prediction_market_soccer.config.leagues import by_api_id
    reader = reader or __import__('prediction_market_soccer.venues.polymarket_global.reader', fromlist=['PolymarketGlobalReader']).PolymarketGlobalReader()
    events, discovery_error = [], None
    resolver = _ClubResolver(conn)
    budget = scope.max_requests
    if market_kind == "match" and budget:
        events, result["discovery"], discovery_error = _catalog(
            reader, "list_match_events", end_date_min=scope.window_start,
            end_date_max=scope.window_end, max_requests=max(0, budget - 3))
        events = events or []
        attempts = result["discovery"]["requests"]
        budget -= attempts
        result["requests"] += attempts
    elif market_kind == "match":
        discovery_error = "request_budget_exhausted"
    advance_indexes = {}

    def save(fid, target_id, status, reason, refs=()):
        item = {"fixture_id": fid, "target_id": target_id, "status": status,
                "reason": reason, "observation_ids": list(refs)}
        result["items"].append(item)
        if status != "complete" and status != "unsupported_market":
            result["complete"] = False
        if state_conn is not None:
            from prediction_market_soccer.util.collection_state import record_attempt
            record_attempt(state_conn, scope, collector, fid, target_id, status, reason, refs)
            state_conn.commit()

    for fid in ids:
        needed = [t for t in targets if (fid, t) in due]
        if not needed:
            continue
        fx = fixtures.get(fid)
        reason, binding = None, None
        comp = by_api_id(fx["league_id"]) if fx else None
        if fx is None or fx.get("status_short") not in _FINISHED or not fx.get("kickoff_ts"):
            reason = "fixture_not_eligible"
        elif not epoch(scope.window_start) <= epoch(fx["kickoff_ts"]) <= epoch(scope.window_end):
            reason = "fixture_outside_window"
        elif market_kind == "match":
            binding, reason = (None, discovery_error) if discovery_error else _identity_for_fixture(fx, comp, cmap, events, resolver)
            if reason == "not_listed":
                discovery = result["discovery"] or {}
                if discovery.get("complete") is not True:
                    reason = "discovery_partial"
                elif discovery.get("identity_complete") is not True:
                    reason = "identity_unresolved"
        else:
            rung = _advance_round_key(comp, fx.get("round"))
            # Global reader only implements qualify-for-league-play contracts.
            if rung != "advance":
                reason = "unsupported_market"
            elif budget <= 0:
                reason = "request_budget_exhausted"
            else:
                try:
                    if comp.key not in advance_indexes:
                        index, status, error = _catalog(reader, "reach_round_index", "advance",
                                                       comp_key=comp.key, max_requests=max(0, budget - 2))
                        advance_indexes[comp.key] = (index or {}, status, error)
                        result["discovery"] = result["discovery"] or {}
                        result["discovery"][comp.key] = status
                        budget -= status["requests"]
                        result["requests"] += status["requests"]
                    index, status, error = advance_indexes[comp.key]
                    sides = {s: index.get(cmap.get(fx[k])) for s, k in [("home", "home_api_id"), ("away", "away_api_id")]}
                    if error:
                        reason = error
                    elif not status.get("complete", False):
                        reason = "discovery_partial"
                    elif not all(sides.values()) or len(set(sides.values())) != 2:
                        reason = "identity_unresolved"
                    else:
                        binding = ({"comp": comp.key, "market_kind": "advance", "round": fx.get("round"), "rung": rung}, sides)
                except Exception as exc:
                    reason = "discovery_failed:" + type(exc).__name__
        if reason:
            for target in needed:
                save(fid, target, "unsupported_market" if reason == "unsupported_market" else "unavailable", reason)
            continue
        ev, sides = binding
        ko = epoch(fx["kickoff_ts"])
        result["matched"] += 1
        receipts = {}
        for side in {t.split(":")[1] for t in needed}:
            if budget <= 0:
                receipts[side] = {"reason": "request_budget_exhausted"}
                continue
            try:
                rec = _receipt(reader, sides[side], ko, fidelity)
                writer.add("series", fid, ko, rec, side=side, market_kind=market_kind,
                           status="invalid" if rec["quality"] == "invalid" else "ok")
                receipts[side] = rec
            except Exception as exc:
                receipts[side] = {"reason": "history_failed:" + type(exc).__name__}
            budget -= 1
            result["requests"] += 1
        states_written = set()
        for target in needed:
            milestone, side, kind = target.split(":")
            rec = receipts[side]
            if "points" not in rec or rec["quality"] != "available":
                save(fid, target, "unavailable", rec.get("reason") or "empty_series")
                continue
            if ticks:
                refs = []
                for point in rec["points"]:
                    sample = sample_price(rec, point["ts"])
                    payload = {**sample, "token_id": sides[side], "venue": "poly_global", "side": side,
                               "market_kind": kind, "target_id": target, "received_at": rec["received_at"],
                               "relative_wall_seconds": point["ts"] - ko, "binding": {**{k: ev.get(k) for k in ("slug", "comp", "date", "identity_version")},
                                           **({"schedule_binding": ev["schedule_binding"]} if "schedule_binding" in ev else {})}}
                    refs.append(writer.add("quote", fid, point["ts"], payload, side=side, market_kind=kind))
                result["rows"] += len(refs)
                # Interval quality, not requested fidelity, establishes path continuity.
                gap = rec["coverage"]["max_gap_s"]
                complete = bool(refs) and gap is not None and gap <= 180 and rec["coverage"]["start_ts"] <= ko - 300 and rec["coverage"]["end_ts"] >= ko + 90 * 60
                save(fid, target, "complete" if complete else "partial", None if complete else "history_gap_exceeds_tolerance", refs)
            else:
                clock = milestone_target(ko, milestone)
                sample = sample_price(rec, clock["target_at"])
                if market_kind == "match" and clock["target_at"] not in states_written:
                    from prediction_market_soccer.util.match_timeline import timeline_state
                    from prediction_market_soccer.util.source_history import event_revision_at
                    at = datetime.fromtimestamp(clock["target_at"], timezone.utc).isoformat()
                    if timeline_provider is not None:
                        state = timeline_provider(fx, clock["target_at"])
                    else:
                        revision = event_revision_at(conn, fid, at)
                        state = timeline_state(revision, home_id=fx["home_api_id"], away_id=fx["away_api_id"],
                                               kickoff=ko, target_at=clock["target_at"], cutoff=clock["target_at"])
                    writer.add("state", fid, clock["target_at"], state, status=state.get("status", "unavailable"))
                    states_written.add(clock["target_at"])
                payload = {**sample, "token_id": sides[side], "venue": "poly_global", "side": side,
                           "market_kind": kind, "target_id": target, "clock": clock,
                           "received_at": rec["received_at"], "binding": {**{k: ev.get(k) for k in ("slug", "comp", "date", "identity_version")},
                                           **({"schedule_binding": ev["schedule_binding"]} if "schedule_binding" in ev else {})}}
                ref = writer.add("quote", fid, clock["target_at"], payload, status=sample["status"], side=side, market_kind=kind)
                result["rows"] += 1
                save(fid, target, "complete" if sample["status"] == "ok" else sample["status"], sample["reason"], [ref])
    result["status"] = "complete" if result["complete"] else "partial"
    return result


def backfill(conn=None, *, scope=None, fixture_ids=None, writer=None, reader=None, state_conn=None,
             limit=None, verbose=False, force=False, since_days=14):
    return collect(conn, scope=scope, fixture_ids=fixture_ids, writer=writer, reader=reader,
                   state_conn=state_conn, limit=limit)


def backfill_advance_pre(conn=None, *, scope=None, fixture_ids=None, writer=None, reader=None,
                         state_conn=None, verbose=False):
    return collect(conn, scope=scope, fixture_ids=fixture_ids, writer=writer, reader=reader,
                   state_conn=state_conn, market_kind="advance")


def main():
    raise SystemExit("Use an explicit CollectionScope and CandidateWriter; historical in-place writes are disabled.")


if __name__ == "__main__":
    main()
