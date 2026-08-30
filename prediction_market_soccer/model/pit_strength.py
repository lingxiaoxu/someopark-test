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


_PRIOR_DAYS_BUILT: set[str] = set()


def pit_prior(conn, comp_key: str, as_of: str):
    """The club prior as it stood on ``as_of``'s date — built, not read from today's file.

    THE single entry point for a point-in-time prior, shared by both replay paths. It was
    two: WalkForwardStrength got this treatment while ops/performance_report._pit_strength
    kept caching `load_prior(league)` under the LEAGUE alone, so the frozen bet ledger —
    the bet log, the three performance tracks, the published Brier — priced every settled
    match with TODAY's prior while its model memo varied by date. Measured on Brasileirão's
    58 settled matches: Brier 0.6364 with today's prior vs 0.6688 with the match-day one,
    paired t = -4.02, and on Argentina the argmax pick flipped on 29 of 90 matches. One
    function now, so a future fix cannot land on one caller and miss the other.

    The per-day build is process-wide (the priors are files, and every caller wants the
    same one for a given day); the returned snapshots are cached by the caller.
    """
    from prediction_market_soccer.ingest.club_prior import build_all, load_prior
    day = (as_of or "")[:10]
    if not day:
        return load_prior(comp_key)
    # DATE-STAMPED files, built once and reused forever. The previous scheme wrote every
    # bucket to the same clubs_<comp>_pit.json and tracked "already built" in process
    # memory — so every settle event, every calibrate run and every OOS run in a fresh
    # process re-ground the whole window from scratch: 2,440 `club_prior built` lines in
    # one day's live log, and the 8-15 minute "cycles" that came with them. A PAST day's
    # prior is immutable (its inputs are that day's fixtures and that day's cached
    # ClubElo CSV), so the file IS the cache; only a never-before-seen day pays the
    # build. TODAY's file is rebuilt once per process — today is still moving.
    suffix = f"{_PIT_SUFFIX}_{day}"
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    from prediction_market_soccer.config import CONFIG as _CFG
    probe = _CFG.paths.priors / f"clubs_{comp_key}{suffix}.json"
    meta_p = _CFG.paths.priors / f".pit_built_{day}.json"
    # The cache is only valid for the DATABASE that built it. The test suite seeds
    # small fixture sets into throwaway DBs and calls this with the same dates the
    # production DB uses; before this fingerprint, a test inherited a file built from
    # production data (or from the previous test) and 11 tests failed only when run
    # together — the classic symptom of state leaking through a shared path.
    _db = ""
    try:
        _db = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    except Exception:
        pass
    _built_ok = False
    if probe.exists() and meta_p.exists():
        try:
            _built_ok = (json.loads(meta_p.read_text(encoding="utf-8")).get("db") == _db)
        except Exception:
            _built_ok = False
    fresh_needed = (day >= today and day not in _PRIOR_DAYS_BUILT)
    if fresh_needed or not _built_ok:
        build_all(conn, as_of=day, suffix=suffix)
        meta_p.write_text(json.dumps({"db": _db, "day": day}), encoding="utf-8")
        _PRIOR_DAYS_BUILT.add(day)
    return load_prior(comp_key, suffix=suffix)


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
        self._failed: set[str] = set()
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
        from prediction_market_soccer.config.leagues import active
        pit_prior(self._conn, comp_key, day)          # builds the whole day once (or reuses the file)
        from prediction_market_soccer.ingest.club_prior import load_prior
        for c in active():
            try:
                self._priors[(c.key, day)] = load_prior(c.key, suffix=f"{_PIT_SUFFIX}_{day}")
            except Exception:      # a competition with no registry rows has no prior
                self._priors[(c.key, day)] = None
        got = self._priors.get((comp_key, day))
        if got is None:
            raise FileNotFoundError(f"no {comp_key} prior for {day}")
        return got

    def for_match(self, comp_key: str, kickoff_ts: str):
        if comp_key in self._failed:
            return None
        as_of = bucket_start(kickoff_ts, bucket_days=self._bucket_days)
        key = (comp_key, as_of)
        if key in self._cache:
            return self._cache[key]
        from prediction_market_soccer.model.squad_strength import build_strength_live
        try:
            sm = build_strength_live(self._conn, self._prior(comp_key, as_of), league=comp_key,
                                     as_of=as_of, xg_form=True)
        except Exception as e:  # noqa: BLE001
            print(f"[pit_strength:{comp_key}] model unavailable ({type(e).__name__}: {e}) — skipped")
            self._failed.add(comp_key)
            return None
        if self._verbose:
            print(f"[pit_strength] {comp_key} @ {as_of[:10]}: {len(sm.ratings)} clubs")
        self._cache[key] = sm
        return sm

    @property
    def n_fits(self) -> int:
        return len(self._cache)
