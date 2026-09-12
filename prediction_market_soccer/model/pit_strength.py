"""model/pit_strength.py — the point-in-time strength model for scoring SETTLED matches.

`build_strength_live` says it plainly: a historical caller MUST pass `as_of=kickoff`,
or the form / xG-form / alt-data blends read the scored match's own later results.
`cached_strength` is the LIVE entry point and deliberately passes `as_of=None` (all
data, correct for a match that has not kicked off yet) — but `oos_eval` and
`calibrate_fit` were calling it to score matches that had ALREADY finished, so the
"out-of-sample" Brier was fitted on the outcomes it was scoring.

Measured on the 636-match window: the live path scored 0.6192 and a model frozen
before the window scored 0.6312, a paired difference of −0.0120 at t = −5.43. Roughly
half of the model's apparent skill over the base rates was the leak reading its own
answers.

Rebuilding per match is 636 fits; the blends move on a weekly cadence (a team plays
once or twice a week), so ratings are refit per ISO week and every match in that week
is priced by the model that knew only what had happened BEFORE the week began. That is
a genuine walk-forward at 12 comps x ~9 weeks = ~2.5 minutes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

# DAY granularity, matching both the World Cup module and this module's own frozen bet
# ledger (ops/performance_report._pit_strength keys on the kickoff DATE). Week buckets were
# never a leak — a Monday-00:00 model cannot see Tuesday's results — but they were coarser
# than the ledger they are compared against, so the same match was priced by two different
# models depending on which report asked. They also threw away up to six days of legitimately
# known results, which understates the model rather than flattering it. Measured cost of the
# change on the 60-day window: 53 builds → 144, and 44 distinct ClubElo dates, each cached on
# disk after its first fetch.
_BUCKET_DAYS = 1
# Priors rebuilt for a past date are written beside the live ones under this suffix, so
# the nightly clubs_<comp>.json the exports read is never overwritten by a backtest.
_PIT_SUFFIX = "_pit"


# Increment when prior reconstruction semantics change; old incomplete caches are invalid.
_PIT_VERSION = "roster-asof-v4-fc-identity"
_PRIOR_DAYS_BUILT: set[tuple[str, str, str]] = set()


def fc_input_fingerprint(conn):
    """Current FC identity/ratings fingerprint, not proof of historical availability."""
    import hashlib
    import sqlite3
    h = hashlib.sha256()
    try:
        for row in conn.execute('SELECT * FROM fc_player ORDER BY rowid'):
            h.update(json.dumps(list(row),default=str,separators=(',',':')).encode())
    except sqlite3.OperationalError:
        pass
    return h.hexdigest()


def _db_identity(conn) -> str:
    from pathlib import Path
    db = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    return str(Path(db).resolve()) if db else f"memory:{id(conn)}"


def pit_out_dir(conn):
    """Separate every database, including in-memory connections, from live priors."""
    import hashlib
    import tempfile
    from pathlib import Path
    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.ingest import store
    db = _db_identity(conn)
    if db == str(Path(store.DB_PATH).resolve()):
        return CONFIG.paths.priors
    key = hashlib.sha256(db.encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "someopark_soccer_pit" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _source_fingerprint(conn, day: str) -> str:
    """Invalidate corrected membership/results and recovered historical Elo sources.

    Future fixtures contribute identity only. Historical strengths still come solely
    from build_all(as_of=day); a fingerprint is an invalidation key, not model input.
    """
    import hashlib
    from prediction_market_soccer.ingest import club_prior
    h = hashlib.sha256(_PIT_VERSION.encode())
    for sql, args in (
        ("SELECT club_id, comp, api_team_id, name FROM club_registry ORDER BY comp, club_id", ()),
        ("SELECT api_id, canonical_team_id FROM team_meta ORDER BY api_id", ()),
        ("SELECT api_id,name FROM team ORDER BY api_id", ()),
        ("SELECT api_id,league_id,season,home_api_id,away_api_id FROM fixture ORDER BY api_id", ()),
        ("SELECT api_id,league_id,season,round,home_api_id,away_api_id,kickoff_ts,status_short,home_goals,away_goals "
         "FROM fixture WHERE substr(kickoff_ts,1,10) < ? ORDER BY api_id", (day,)),
    ):
        for row in conn.execute(sql, args):
            h.update(json.dumps(list(row), default=str, separators=(",", ":")).encode())
    # FC26 is a current, unversioned feature source: invalidate corrections without
    # claiming it was historically available. Historical PIT evidence remains unproven.
    h.update(fc_input_fingerprint(conn).encode())
    from prediction_market_soccer.config.leagues import active
    for comp in active():
        for season in [comp.season - 1]:
            for row in conn.execute(
                    "SELECT team_api_id,points,rank,played FROM standing WHERE league_id=? AND season=? ORDER BY team_api_id",
                    (comp.api_football_id, season)):
                h.update(repr((comp.key, season, tuple(row))).encode())
    elo_cutoff = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    for p in sorted(club_prior._PRIORS.glob("clubelo_*")):
        # Historical builds may use an earlier valid website snapshot. Never pin
        # an outage/default forever when a legitimate historical source arrives.
        if p.is_file() and p.name[8:18] <= elo_cutoff:
            h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()


def pit_prior(conn, comp_key: str, as_of: str):
    """Build a date-specific prior; missing history NEVER loads today's live prior."""
    from prediction_market_soccer.ingest.club_prior import build_all, load_prior
    day = str(as_of or "")[:10]
    datetime.strptime(day, "%Y-%m-%d")   # empty/invalid dates cannot mean 'live'
    out_dir = pit_out_dir(conn)
    suffix = f"{_PIT_SUFFIX}_{day}"
    probe = out_dir / f"clubs_{comp_key}{suffix}.json"
    meta_p = out_dir / f".pit_built_{day}.json"
    fingerprint = _source_fingerprint(conn, day)
    identity = _db_identity(conn)
    expected = {"db": identity, "day": day, "version": _PIT_VERSION,
                "source_fingerprint": fingerprint}
    try:
        valid = probe.exists() and json.loads(meta_p.read_text()) == expected
    except (OSError, ValueError):
        valid = False
    if not valid:
        # A failed build must not leave yesterday's success marker usable.
        meta_p.unlink(missing_ok=True)
        for old in out_dir.glob(f"clubs_*{suffix}.json"):
            old.unlink(missing_ok=True)
        build_all(conn, as_of=day, suffix=suffix, out_dir=out_dir, point_in_time=True)
        if not probe.exists():
            raise FileNotFoundError(f"historical prior unavailable: {comp_key}@{day}")
        # A build may have recovered an eligible historical Elo cache.
        expected["source_fingerprint"] = _source_fingerprint(conn, day)
        meta_p.write_text(json.dumps(expected), encoding="utf-8")
        _PRIOR_DAYS_BUILT.add((identity, day, expected["source_fingerprint"]))
    return load_prior(comp_key, suffix=suffix, out_dir=out_dir, validate_roster=False)


def bucket_start(kickoff_ts: str, *, bucket_days: int = _BUCKET_DAYS) -> str:
    """ISO timestamp of the start of ``kickoff_ts``'s walk-forward bucket.

    Anchored on ISO Monday so the boundary is a real football week rather than an
    artefact of when the evaluation happened to run.
    """
    ts = str(kickoff_ts).replace(" ", "T")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket_days == 7:
        day -= timedelta(days=day.weekday())          # back to Monday
    else:
        epoch = datetime(1970, 1, 5, tzinfo=timezone.utc)   # a Monday
        day -= timedelta(days=(day - epoch).days % bucket_days)
    return day.isoformat()


class WalkForwardStrength:
    """Per-(competition, week) strength models, built lazily and cached.

    ``for_match(comp_key, kickoff_ts)`` returns the model that knew only what had
    happened before that match's week — the model an honest forecaster would have
    had. Returns None if the competition's prior cannot be loaded (same
    skip-and-report contract the callers already had around ``cached_strength``).
    """

    def __init__(self, conn, *, bucket_days: int = _BUCKET_DAYS, verbose: bool = False):
        self._conn = conn
        self._bucket_days = bucket_days
        self._verbose = verbose
        self._cache: dict[tuple[str, str], object] = {}
        self._failed: set[tuple[str, str]] = set()
        # (competition, day) → the prior as it stood that day. NOT a set of days: the
        # per-day files share one path per competition and are overwritten in place.
        self._priors: dict[tuple[str, str], object] = {}

    def _prior(self, comp_key: str, as_of: str):
        """This bucket's prior, held per (competition, day).

        Keying the cache on the date alone was a look-ahead leak: build_all writes each
        competition to ONE fixed path, so only a single generation can exist and a later
        competition short-circuiting on "this date was already built" loaded whatever
        generation happened to be on disk — measured, epl@2026-08-10 receiving the prior
        built for 2026-08-17, a week AFTER the matches it was pricing.
        """
        day = as_of[:10]
        hit = self._priors.get((comp_key, day))
        if hit is not None:
            return hit
        got = pit_prior(self._conn, comp_key, day)
        # A successful build validates the whole date generation. Capture its
        # available competitions together, without reading any live-prior fallback.
        from prediction_market_soccer.config.leagues import active
        from prediction_market_soccer.ingest.club_prior import load_prior
        out_dir = pit_out_dir(self._conn)
        for c in active():
            try:
                self._priors[(c.key, day)] = load_prior(
                    c.key, suffix=f"{_PIT_SUFFIX}_{day}", out_dir=out_dir, validate_roster=False)
            except (FileNotFoundError, ValueError):
                pass
        self._priors[(comp_key, day)] = got
        return got

    def for_match(self, comp_key: str, kickoff_ts: str):
        as_of = bucket_start(kickoff_ts, bucket_days=self._bucket_days)
        key = (comp_key, as_of)
        if key in self._failed:
            return None
        if key in self._cache:
            return self._cache[key]
        from prediction_market_soccer.model.squad_strength import build_strength_live
        try:
            sm = build_strength_live(self._conn, self._prior(comp_key, as_of), league=comp_key,
                                     as_of=as_of, xg_form=True)
        except Exception as e:  # noqa: BLE001
            print(f"[pit_strength:{comp_key}] model unavailable ({type(e).__name__}: {e}) — skipped")
            self._failed.add(key)
            return None
        if self._verbose:
            print(f"[pit_strength] {comp_key} @ {as_of[:10]}: {len(sm.ratings)} clubs")
        self._cache[key] = sm
        return sm

    @property
    def n_fits(self) -> int:
        return len(self._cache)
