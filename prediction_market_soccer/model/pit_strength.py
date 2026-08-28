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

from datetime import datetime, timedelta, timezone

_BUCKET_DAYS = 7
# Priors rebuilt for a past date are written beside the live ones under this suffix, so
# the nightly clubs_<comp>.json the exports read is never overwritten by a backtest.
_PIT_SUFFIX = "_pit"


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
        self._prior_days: set[str] = set()

    def _prior(self, comp_key: str, as_of: str):
        """The club prior as it stood at ``as_of`` — built, not loaded from today's file.

        `load_prior` reads data/priors/clubs_<comp>.json, which is rebuilt every night
        against the LIVE standings and today's ClubElo. Handing that to a walk-forward
        model made the ratings' own anchor the leak: for the mid-season competitions the
        anchor is dominated by the current table, and a July model was anchored on the
        August one (measured corr with the realised season 0.986).

        Rebuilding per bucket is affordable because the expensive part is one ClubElo
        CSV per date and that is cached on disk (19.7s cold, 0.1s warm), so a nine-week
        window costs nine fetches once and nothing thereafter.
        """
        day = as_of[:10]
        if day in self._prior_days:
            from prediction_market_soccer.ingest.club_prior import load_prior
            return load_prior(comp_key, suffix=_PIT_SUFFIX)
        from prediction_market_soccer.ingest.club_prior import build_all, load_prior
        build_all(self._conn, as_of=day, suffix=_PIT_SUFFIX)
        self._prior_days.add(day)
        return load_prior(comp_key, suffix=_PIT_SUFFIX)

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
