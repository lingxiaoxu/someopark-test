"""Build synthetic worlds for one Kalshi series and score them (S5/S7 driver).

This is the composition of the four pieces that already exist — `panel` builds the PIT
macro history, `generator` draws forward paths from it, `worlds` writes a path into a
schema-identical db, `book` transplants a real market onto the synthetic events — into the
one operation everything downstream wants: *give me N scored synthetic events for series X
as of date D*.

Three things here are not in the smoke script it grew from, and each is a correctness
requirement rather than a convenience:

**The generation anchor is the last OBSERVATION, not the last training anchor.**
`PanelData.anchors[-1]` sits H periods before the end of history by construction, so a path
generated from it is a window the generator was fitted on — a "synthetic future" the model
has already seen. Measured on claims, using the wrong one biased `z_y` by +1.18; using the
right one leaves +0.47.

**Every generated column is written, not just the one that settles.** The energy panel
generates wti/natgas/rbob/gas_retail because the energy model reads all four. Writing only
the settling column leaves the others empty after the splice, and the model then runs on a
world where three of its inputs stopped existing.

**Futures paths are expanded to daily bars.** `_gbm_futures` estimates sigma from DAILY log
returns over a 60+ bar window and counts business days to settle. Handing it 13 weekly bars
would have it read weekly moves as daily ones — a sigma roughly sqrt(5) too large, on a
history a quarter as long as production gives it. The expansion is a geometric Brownian
bridge, which is the conditional law of a GBM given both endpoints, so the Friday closes the
contracts settle on are preserved EXACTLY while the intervening days carry the daily
volatility the model is entitled to measure.

Scope note. `KXAAAGASW` settles on the AAA national average, and `AAA_DAILY` holds 21
observations, all after 2026-07-31. There is no history to generate it from, and its model
(`energy._aaa_drift_fit`) predicts the AAA-minus-GASREGW gap specifically — so a synthetic
world that set AAA equal to the generated GASREGW would hand that regression a target that
is identically zero, and one that resampled the gap independently would destroy the very
dependence the model exists to exploit. Either choice fabricates the answer. The series is
therefore out of scope for generation, which is a fact about the data rather than a
simplification, and it is stated here rather than discovered later.
"""
from __future__ import annotations

import concurrent.futures as cf
import importlib
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.model.common import grid_pmf
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.research import pnl_score as ps
from prediction_market_macro.research.synth import book as B
from prediction_market_macro.research.synth import generator as G
from prediction_market_macro.research.synth import panel as P
from prediction_market_macro.research.synth import worlds as W
from prediction_market_macro.util.periods import kalshi_period_to_key

UTC = timezone.utc


# ── which panel column becomes which world table ─────────────────────────────
@dataclass(frozen=True)
class Sink:
    """Where a generated panel column lands in a world db."""
    kind: str        # "fred" | "fut"
    name: str        # sid or root


# Keyed by panel, then by the panel's own column name. Every GENERATED column of a panel
# must appear here or `_sinks` raises: a column the generator pays to produce and nobody
# writes is a silently empty stretch of synthetic history.
SINKS: dict[str, dict[str, Sink]] = {
    "claims_weekly": {
        "claims": Sink("fred", "ICSA"),
    },
    "energy_weekly": {
        "wti": Sink("fut", "CL"),
        "natgas": Sink("fut", "NG"),
        "rbob": Sink("fut", "RB"),
        "gas_retail": Sink("fred", "GASREGW"),
    },
    # The monthly panels — the ones the sample gate actually binds on. `claims` and
    # `gas_retail` appear here as MONTHLY MEANS of series that print weekly, which is what
    # `Column.agg == "mean"` records, and they are written back out week by week rather than
    # as one observation on the first of the month. See `_sub_monthly` for why that is not
    # cosmetic: `payrolls` reads ICSA as `icsa.rolling(4).mean()` and its own 4-week change
    # `c4.iloc[-1] - c4.iloc[-5]`, and `cpi._gas_effect` weights the current month by
    # `min(len(cur)/4.3, 1.0)` — one observation a month makes all three read as if the
    # series had gone quiet.
    "labor_monthly": {
        "payems": Sink("fred", "PAYEMS"),
        "unrate": Sink("fred", "UNRATE"),
        "claims": Sink("fred", "ICSA"),
    },
    "inflation_monthly": {
        "cpi": Sink("fred", "CPIAUCSL"),
        "cpi_core": Sink("fred", "CPILFESL"),
        "pce_core": Sink("fred", "PCEPILFE"),
        "gas_retail": Sink("fred", "GASREGW"),
    },
    # The quarterly panel — one column, and the whole of KXGDP (#183). The advance print is
    # written straight to its own FRED sid; what makes this panel different from the other
    # five is that the MODEL's anchor is not the sid at all but a published forecast of it,
    # which nothing here generates as a column. See `NOWCASTS`.
    "gdp_quarterly": {
        "gdp": Sink("fred", "A191RL1Q225SBEA"),
    },
}

@dataclass(frozen=True)
class Settle:
    """How a series' settlement value is read off a generated column's LEVEL path.

    The settlement transform is independent of the panel's stationarity transform and must
    not be confused with it: `unrate` enters the panel as a first difference because that is
    what is stationary, while KXU3 settles on the LEVEL. Getting these backwards produces a
    world that is internally consistent and scores a market nobody trades, which is the kind
    of error that survives review — hence `verify_settle`, which checks every one of these
    against the outcomes of real settled events rather than against an argument.

      level    the level itself                  KXU3 4.05, KXWTIW 83.20
      diff     level_t - level_{t-1}, x scale    KXPAYROLLS 150500 jobs from PAYEMS' 1000s
      pct100   (level_t/level_{t-1} - 1) * 100   KXCPI 0.45 %MoM from the CPI index
      yoy100   (level_t/level_{t-12} - 1) * 100  KXCPIYOY 4.15 %YoY
    """
    panel: str
    column: str
    how: str                 # level | diff | pct100 | yoy100
    scale: float = 1.0
    lookback: int = 1        # periods of real history the transform needs before the path

    def level_step(self, round_rule: float) -> float | None:
        """The grid to round the generated LEVEL to, or None when no such grid exists.

        The generated level is what gets written into the world, and the settlement value is
        then derived from it — so the two are the same object however this comes out. What
        rounding buys is that the derived value lands on the ladder's own grid instead of
        somewhere between two strikes, which is how the real series print: WTI to the cent,
        ICSA to the thousand, PAYEMS to the thousand jobs (`round_rule / scale`, since the
        level is carried in PAYEMS' thousands).

        For `pct100`/`yoy100` there is no such grid: a CPI index rounded to 0.1 moves the
        MoM it implies by about 0.03pp, a third of a ladder bucket, so rounding the level
        would inject noise into the settlement rather than align it. Those are left alone
        and settle on the exact ratio, which is what BLS publishes anyway.
        """
        return round_rule / self.scale if self.how in ("level", "diff") else None


SETTLES: dict[str, Settle] = {
    # weekly
    "KXJOBLESSCLAIMS": Settle("claims_weekly", "claims", "level"),
    "KXWTIW": Settle("energy_weekly", "wti", "level"),
    "KXNATGASW": Settle("energy_weekly", "natgas", "level"),
    # monthly — the series the sample gate actually binds on
    "KXPAYROLLS": Settle("labor_monthly", "payems", "diff", scale=1000.0),
    "KXU3": Settle("labor_monthly", "unrate", "level"),
    "KXCPI": Settle("inflation_monthly", "cpi", "pct100"),
    "KXCPICORE": Settle("inflation_monthly", "cpi_core", "pct100"),
    "KXCPIYOY": Settle("inflation_monthly", "cpi", "yoy100", lookback=12),
    "KXCPICOREYOY": Settle("inflation_monthly", "cpi_core", "yoy100", lookback=12),
    "KXPCECORE": Settle("inflation_monthly", "pce_core", "pct100"),
    # quarterly — the advance estimate settles on the growth RATE itself, which is what
    # A191RL1Q225SBEA publishes and what the panel carries as `transform="level"`. There is
    # no second transform here and that is the point: for KXPAYROLLS the panel holds a level
    # and the market settles on a change, for KXGDP the published series IS the change.
    "KXGDP": Settle("gdp_quarterly", "gdp", "level"),
    # KXFED is absent on purpose: it settles on a policy decision, not a macro variable any
    # panel generates, and §5b measured it as the one categorical series with no numeric book
    # descriptor — so it has no donors either. Two independent reasons, same answer.
}

_HOW = ("level", "diff", "pct100", "yoy100")


def outcome_path(levels: pd.Series, st: Settle) -> pd.Series:
    """Apply a settlement transform to a level series that already carries its own lookback.

    `levels` must run from at least `st.lookback` periods before the first generated period
    through the last, because `diff`/`pct100`/`yoy100` all read backwards — for the FIRST
    generated period that lookback is real history, and dropping it would silently start the
    synthetic sample one period late.
    """
    if st.how == "level":
        return levels * st.scale
    if st.how == "diff":
        return levels.diff() * st.scale
    if st.how == "pct100":
        return (levels / levels.shift(1) - 1.0) * 100.0 * st.scale
    if st.how == "yoy100":
        return (levels / levels.shift(12) - 1.0) * 100.0 * st.scale
    raise ValueError(f"outcome_path: unknown transform {st.how!r}, expected one of {_HOW}")


def _sinks(panel: str) -> dict[str, Sink]:
    spec = P.PANELS[panel]
    have = SINKS.get(panel, {})
    missing = [c.name for c in spec.gen_columns if c.name not in have]
    if missing:
        raise ValueError(
            f"build: panel {panel!r} generates {missing} with nowhere to write them — a "
            "generated column that never reaches the world is history that silently stops "
            "at the splice for every model reading it")
    return {c.name: have[c.name] for c in spec.gen_columns}


# ── clocks, measured from the real db rather than tabulated ──────────────────
# How long before its own number is published a synthetic market stops trading. Kalshi's
# own convention, read off the db: KXCPI closes 12:25 against a 12:30 BLS release, KXPAYROLLS
# 12:29, KXJOBLESSCLAIMS 12:25. Five minutes is inside that range and, more importantly, it
# makes "the market closes before the answer exists" true BY CONSTRUCTION for every series
# and every frequency, instead of true by a coincidence between a tabulated close time and a
# measured publication lag. That coincidence was doing real work in the weekly build and it
# would have broken on the monthly one in the leaking direction: a KXCPI event stamped at
# Kalshi's 12:25 against a synthetic release stamped 12:00 would have had the model read the
# settlement value before quoting it.
CLOSE_LEAD = timedelta(minutes=5)


@dataclass(frozen=True)
class Clock:
    """When a sink's value for a panel period becomes knowable inside a world.

    One object drives three things that must agree or the world leaks: the splice (where
    real history stops), the `knowledge_time` written on every generated observation, and
    the close time of every synthetic event. They were three separate calculations in the
    first build, which is how the coincidence above came to be load-bearing.
    """
    kind: str                # "fred" | "fut"
    lag_days: int
    hour: int
    weekday: int | None      # the sid's own dating convention, weekly panels only


def _fred_weekday(conn: sqlite3.Connection, sid: str) -> int:
    """Modal weekday of `sid`'s real `event_time`s.

    A weekly panel stamps W-SAT, but ICSA dates its weeks on the Saturday and GASREGW on the
    Monday. Writing a generated GASREGW week on Saturday would date it five days early and
    then `publication_lag` — measured on Monday-dated prints — would stamp its knowledge time
    five days early too, which is a point-in-time leak assembled out of two correct pieces.
    """
    rows = conn.execute(
        "SELECT event_time FROM fred_obs WHERE sid=? ORDER BY event_time DESC LIMIT 120",
        (sid,)).fetchall()
    days = []
    for (t,) in rows:
        try:
            days.append(datetime.fromisoformat(str(t)).weekday())
        except ValueError:
            continue
    if not days:
        raise ValueError(f"_fred_weekday: no parseable event_time for {sid!r}")
    return Counter(days).most_common(1)[0][0]


def _on_weekday(idx: pd.DatetimeIndex, weekday: int) -> pd.DatetimeIndex:
    """Move each W-SAT bucket end to the date INSIDE that bucket with the given weekday.

    The bucket is [d-6, d], so the member with weekday `w` is `d - ((5 - w) mod 7)`.
    """
    return idx - pd.Timedelta(days=(5 - weekday) % 7)


@dataclass(frozen=True)
class Cadence:
    """What a panel's period frequency decides — one field per decision (#212).

    Until this existed the whole thing was a single predicate, `_weekly(spec)`, read at five
    call sites that ask five *different* questions. With exactly two frequencies in the
    project the five answers were perfectly correlated, so one bit carried all of them and
    nothing was wrong — it was merely unfalsifiable. KXGDP breaks the correlation: quarterly
    data under a token named for the RELEASE DATE, which is the weekly convention on the
    token axis and the monthly convention on every other one. A boolean cannot express that,
    and writing `or _quarterly(spec)` at each site would leave whoever adds the sixth
    frequency to rediscover which of the five flip. Ten live series build through this path,
    so the axes are separated first and the panel added second.

    `registry_cadence` is the `SeriesSpec.cadence` string a series settling off this panel
    must carry. `build_worlds` checks the two agree rather than assuming it, because a
    mismatch names every generated event wrong and surfaces as "0 events generated", which
    looks like a modelling failure.

    `token` is `"close_date"` when a market is named for the day it closes (KXWTIW-26MAY2914,
    KXGDP-27JAN28) and `"reference_period"` when it is named for the period the number
    describes (KXCPI-26JUL is July's CPI, released in August). It drives `_token` and its
    inverse `_panel_period` from the same field, so the two cannot drift apart.

    `dates_within` is whether a FRED sink dates its observation somewhere INSIDE the panel's
    bucket rather than on the bucket's own label. Weekly does, and the two conventions
    disagree: a W-SAT bucket holds ICSA on the Saturday and GASREGW on the Monday. Monthly
    and quarterly do not — FRED labels both by the first day of the period, which is exactly
    how the panel labels them, so the observation moves nowhere.

    `expander` names the routine that turns one generated bucket value into the finer
    observations a model reads, or None when this build has none for that cadence. It is
    deliberately NOT "does this cadence aggregate a finer source": `energy_weekly_wide`
    carries DGS2/DGS10 as weekly means of a DAILY series and has no expander either, and the
    old `not _weekly(psp)` test silently gave that the same answer for a different reason.
    Naming the gap as a None is the point; closing it for weekly panels is a separate
    question and is not decided here.
    """
    registry_cadence: str
    token: str                 # "close_date" | "reference_period"
    dates_within: bool
    expander: str | None
    period_floor: str | None   # pandas Period alias, for the reference_period inverse


CADENCES: dict[str, Cadence] = {
    "W":  Cadence("weekly",    "close_date",       True,  None,          None),
    "MS": Cadence("monthly",   "reference_period", False, "sub_monthly", "M"),
    "QS": Cadence("quarterly", "close_date",       False, None,          None),
}


def cadence(spec: P.PanelSpec) -> Cadence:
    """The `Cadence` for a panel, keyed on the family of its pandas offset alias.

    Split on "-" so every weekly anchor ("W-SAT", "W-MON") lands on the one weekly entry:
    which weekday the bucket ENDS on is a panel's business and changes nothing about the
    four axes above.
    """
    key = spec.freq.upper().split("-")[0]
    if key not in CADENCES:
        raise ValueError(
            f"build: panel {spec.name!r} has frequency {spec.freq!r}, which is not one of "
            f"{sorted(CADENCES)}. Adding one means deciding all four axes of `Cadence` "
            "explicitly — that is the whole reason this table exists rather than a boolean")
    return CADENCES[key]


def clock(src: sqlite3.Connection, spec: P.PanelSpec, sk: Sink) -> Clock:
    """Measure `sk`'s publication clock. Nothing here is tabulated."""
    if sk.kind == "fred":
        lag, hour = W.publication_lag(src, sk.name)
        return Clock("fred", lag, hour,
                     _fred_weekday(src, sk.name) if cadence(spec).dates_within else None)
    # A futures session is knowable the evening it closes; `worlds.write_fut` stamps that
    # same hour, so the two cannot drift apart.
    return Clock("fut", 0, W.FUT_CLOSE_HOUR, None)


def observation_date(spec: P.PanelSpec, ck: Clock, period: pd.Timestamp) -> pd.Timestamp:
    """The date a sink's observation for `period` carries in the world.

    A weekly panel stamps W-SAT while ICSA dates its weeks on the Saturday and GASREGW on
    the Monday, so a weekly FRED observation moves to its own weekday inside the bucket. A
    monthly panel is already labelled the way FRED labels monthly observations (the first of
    the month), so it moves nowhere. A futures bar lands on the last business day of the
    bucket — the Friday of a W-SAT week, which is the session KXWTIW and KXNATGASW settle
    on.
    """
    if ck.kind == "fut":
        return pd.bdate_range(end=period, periods=1)[0]
    if ck.weekday is None:
        return period
    return _on_weekday(pd.DatetimeIndex([period]), ck.weekday)[0]


def knowable_at(spec: P.PanelSpec, ck: Clock, period: pd.Timestamp) -> datetime:
    """When `period`'s value for this sink becomes readable — exactly the `knowledge_time`
    `worlds.write_fred` / `write_fut` will stamp on it, computed the same way."""
    d = observation_date(spec, ck, period)
    if ck.kind == "fred":
        d = d + pd.Timedelta(days=ck.lag_days)
    return d.to_pydatetime().replace(hour=ck.hour, minute=0, second=0, microsecond=0,
                                     tzinfo=UTC)


# ── weekly -> daily, for the roots that settle on a daily close ──────────────
def _sigma_daily(conn: sqlite3.Connection, root: str, asof: datetime,
                 n: int = 250) -> float:
    """Daily log-return sd of `root` over the `n` real bars before `asof`."""
    rows = conn.execute(
        "SELECT close FROM fut_daily WHERE root=? AND knowledge_time<=?"
        " ORDER BY event_time DESC LIMIT ?", (root, asof.isoformat(), n)).fetchall()
    px = np.array([r[0] for r in reversed(rows) if r[0] is not None and r[0] > 0])
    if len(px) < 30:
        raise ValueError(f"_sigma_daily: only {len(px)} usable bars of {root!r} before "
                         f"{asof} — cannot scale a bridge on that")
    return float(np.std(np.diff(np.log(px)), ddof=1))


def _daily_bridge(weekly: pd.Series, sigma_d: float,
                  rng: np.random.Generator) -> pd.Series:
    """Business-daily closes through every point of `weekly`, pinned exactly at each.

    A geometric Brownian bridge: in log space, linear interpolation between consecutive
    weekly closes plus `sigma_d * (S_j - (j/m) S_m)` where `S` is a random walk of iid
    N(0, 1) steps. That is the exact conditional law of a GBM given both endpoints, so the
    Friday closes the contracts settle on come through untouched — the settlement value and
    the bar the model reads are the same number by construction, not by rounding luck —
    while the days between carry daily volatility instead of a straight line.

    A straight line is the failure mode worth naming: `_gbm_futures` takes sigma from the MAD
    of daily log returns, and on a linearly interpolated path those are constant within a
    week, so the MAD collapses toward zero and the model would quote a market it is certain
    about. The synthetic world would then look wildly profitable for reasons that are
    entirely an artifact of interpolation.
    """
    if len(weekly) < 2:
        raise ValueError("_daily_bridge: need at least an anchor and one forward week")
    idx = pd.DatetimeIndex(weekly.index)
    out: dict[pd.Timestamp, float] = {idx[0]: float(weekly.iloc[0])}
    for k in range(len(idx) - 1):
        t0, t1 = idx[k], idx[k + 1]
        lo, hi = float(np.log(weekly.iloc[k])), float(np.log(weekly.iloc[k + 1]))
        # business days strictly inside (t0, t1]; the right end is the pinned weekly close
        span = pd.bdate_range(t0 + pd.Timedelta(days=1), t1)
        m = len(span)
        if m == 0:
            continue
        steps = rng.standard_normal(m)
        s = np.cumsum(steps)
        j = np.arange(1, m + 1)
        bridge = sigma_d * (s - (j / m) * s[-1])
        vals = np.exp(lo + (hi - lo) * (j / m) + bridge)
        for ts, v in zip(span, vals):
            out[ts] = float(v)
        out[t1] = float(weekly.iloc[k + 1])          # pin, exactly
    return pd.Series(out).sort_index()


# ── monthly -> weekly, for the sub-monthly sources a monthly panel carries as a mean ─────
def _sigma_within(conn: sqlite3.Connection, sid: str, asof: datetime,
                  n_months: int = 60) -> float:
    """Log sd of a weekly print around its OWN month's mean, from real history.

    Not the week-to-week sd, which contains the monthly movement the generator is already
    producing. What is needed here is only the part the monthly aggregate threw away: how
    far a single week sits from the month it belongs to. Measured rather than assumed, for
    the same reason `_sigma_daily` is — the failure mode of guessing it is a path whose
    within-month variation is too small, which makes every model reading the weekly series
    more certain than it has any right to be.
    """
    rows = conn.execute(
        "SELECT event_time, value FROM fred_obs WHERE sid=? AND knowledge_time<=?"
        " AND value>0 ORDER BY event_time DESC LIMIT ?",
        (sid, asof.isoformat(), n_months * 6)).fetchall()
    if len(rows) < 24:
        raise ValueError(f"_sigma_within: only {len(rows)} usable prints of {sid!r} before "
                         f"{asof} — cannot measure a within-month spread on that")
    ser = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows}).sort_index()
    ser = ser[~ser.index.duplicated(keep="last")]
    lg = np.log(ser)
    dev = lg - lg.groupby(lg.index.to_period("M")).transform("mean")
    # months with a single print contribute an exact zero and would drag the sd down
    keep = lg.groupby(lg.index.to_period("M")).transform("count") > 1
    dev = dev[keep]
    if len(dev) < 12:
        raise ValueError(f"_sigma_within: {sid!r} has {len(dev)} prints in multi-print "
                         "months — not enough to measure a within-month spread")
    return float(np.std(dev, ddof=1))


def _largest_remainder(vals: np.ndarray, grid: float, total: float) -> np.ndarray | None:
    """Round `vals` onto `grid` so that their SUM is EXACTLY `total`, or None if it cannot be.

    Plain rounding cannot do this — n independent roundings move the sum by up to n/2 grid
    cells — and the sum is the thing `_sub_monthly` exists to preserve. Largest remainder
    floors every value and then hands the +1s to the largest fractional parts, so no value
    moves by more than one cell and the total is hit by construction.

    It returns None rather than an approximation when `total` is not itself a multiple of
    `grid`, because at that point one of the two properties HAS to give and the caller is the
    only place that can decide which. Silently keeping the grid would break the pin that makes
    the world internally consistent; silently keeping the pin would emit weekly prints the
    publisher cannot print. The caller keeps the pin and says so.
    """
    n = int(round(total / grid))
    if abs(n * grid - total) > 1e-6 * max(1.0, abs(total)):
        return None
    q = np.floor(vals / grid)
    rem = n - int(q.sum())
    if rem < 0 or rem > len(vals):
        return None
    if rem:
        q[np.argsort(-(vals / grid - q))[:rem]] += 1
    out = q * grid
    return out if np.all(out > 0) else None


def _emit(out: dict, stamps, vals: np.ndarray, grid: float | None) -> None:
    """Write the UNPINNED weeks of a month. They carry no total to preserve, so each one is
    rounded on its own — dropping the grid here as well would leave the only off-grid prints
    in the world sitting in the months that already had something wrong with them."""
    if grid:
        vals = np.round(vals / grid) * grid
    for ts, v in zip(stamps, vals):
        out[ts] = float(v)


def _sub_monthly(monthly: pd.Series, weekday: int, sigma_w: float,
                 rng: np.random.Generator, *, fixed: pd.Series | None = None,
                 grid: float | None = None, log=None) -> pd.Series:
    """Weekly prints through a generated monthly MEAN, pinned to that mean exactly.

    `monthly` is indexed by period start (MS). Each month's prints land on `weekday`, the
    sid's own dating convention as measured by `_fred_weekday`. Within a month the values
    are a smooth log-space interpolation between neighbouring monthly means plus a mean-zero
    wiggle at `sigma_w`, and the whole month is then rescaled so its ARITHMETIC mean is the
    generated value — arithmetic because that is the aggregation `Column.agg == "mean"`
    applied when the panel was built. The pin is what keeps the world internally consistent:
    the generator learned how `claims` co-moves with `payems`, and a world whose ICSA does
    not aggregate back to the generated `claims` has broken exactly that co-movement, which
    is the reason the payrolls model is being shown a claims path at all.

    `fixed` carries real prints that are already in the world for a straddling first month —
    weeks that were knowable before the splice and must not be rewritten. They are held and
    the remaining weeks absorb the whole adjustment, so the month still aggregates to its
    generated value. With fewer than two free weeks that solve puts the entire monthly
    residual on one print, so the pin is dropped for that month and said so rather than
    producing a spike.

    `grid` is the SOURCE print grid — ICSA prints multiples of 1000, GASREGW of 0.001 — and
    passing it makes the weekly prints land on it. Without it this function emits continuous
    floats, which is what a settlement reading the WEEKLY series (KXJOBLESSCLAIMS) sees, and
    no real weekly claims print has ever been 237_412.83.

    That fix is unlocked by PR-17 and was impossible before it. The pin requires
    `sum(week) = n * mean`, so the grid can only survive the pin if `n * mean` is itself a
    multiple of the grid. Under the pooled lattice the monthly mean was a multiple of
    `g/20`, and `n * g/20` is not a multiple of `g` for n = 4 or 5 — the two properties were
    arithmetically incompatible and rounding here would have broken the pin every month.
    Under the period-conditional lattice the mean is a multiple of `g/n(period)`, so
    `n * mean` is an exact multiple of `g` and both hold at once. `_largest_remainder` is
    what makes "both" exact rather than approximate. Held real prints are on the grid too
    (that is `_sub_period_rule`'s condition 2), so subtracting them keeps the target on it.
    """
    idx = pd.DatetimeIndex(monthly.index)
    lg = np.log(monthly.astype(float))
    out: dict[pd.Timestamp, float] = {}
    unpinned: list[str] = []
    off_grid: list[str] = []
    for k, per in enumerate(idx):
        days = pd.date_range(per, per + pd.offsets.MonthEnd(0), freq="D")
        days = pd.DatetimeIndex([d for d in days if d.weekday() == weekday])
        held = pd.Series(dtype=float) if fixed is None else fixed.reindex(days).dropna()
        free = days.difference(held.index)
        if len(free) == 0:
            continue
        # log-space backbone: straight line between this month's mean and the next one's,
        # so consecutive months connect instead of stepping at the boundary. `payrolls`
        # reads a 4-week rolling mean straight across that boundary.
        nxt = lg.iloc[k + 1] if k + 1 < len(lg) else lg.iloc[k]
        frac = (np.arange(len(days)) + 0.5) / len(days)
        back = np.exp(lg.iloc[k] + (nxt - lg.iloc[k]) * (frac - 0.5) * 0.5)
        wig = rng.standard_normal(len(days))
        vals = pd.Series(back * np.exp(sigma_w * (wig - wig.mean())), index=days)
        target = float(monthly.iloc[k]) * len(days)
        if len(held):
            if len(free) < 2:
                unpinned.append(str(per.date()))
                _emit(out, free, vals[free].to_numpy(dtype=float), grid)
                continue
            target -= float(held.sum())
        raw = float(vals[free].sum())
        if raw <= 0 or target <= 0:
            unpinned.append(str(per.date()))
            _emit(out, free, vals[free].to_numpy(dtype=float), grid)
            continue
        scaled = vals[free].to_numpy(dtype=float) * target / raw
        if grid:
            snapped = _largest_remainder(scaled, grid, target)
            if snapped is None:
                off_grid.append(str(per.date()))   # pin kept, grid dropped — see the docstring
            else:
                scaled = snapped
        for ts, v in zip(free, scaled):
            out[ts] = float(v)
    if unpinned and log:
        log(f"    sub-monthly: {len(unpinned)} month(s) left unpinned "
            f"({', '.join(unpinned)}) — too few free weeks to absorb the residual")
    if off_grid and log:
        log(f"    sub-monthly: {len(off_grid)} month(s) written OFF the {grid:g} print grid "
            f"({', '.join(off_grid)}) — the month's pinned total is not a multiple of it, so "
            "the pin was kept and the grid dropped")
    return pd.Series(out).sort_index()


def _sub_print_grid(pdata, col: str, weekday: int) -> float | None:
    """The source print grid `_sub_monthly` may round onto, or None if none was measured.

    Read off the PANEL's lattice rather than re-derived from the source, because the entry is
    the same measurement the monthly level was quantised with and the two have to agree for
    the pin to survive the rounding at all (see `_sub_monthly`). A column with no
    `sub_period` — DGS2/DGS10, or any column whose calendar could not be established — gets
    None and keeps writing continuous weekly values, which is a defect that is recorded
    rather than papered over with a grid nobody measured.

    The weekday is checked, not assumed. `_fred_weekday` dates the prints and the lattice
    rule counts them; if those two disagree then `n` weeks of `grid` is not the month's
    total and the largest-remainder solve would silently move the month's mean.
    """
    entry = (getattr(pdata, "lattice", None) or {}).get(col) or {}
    rule = entry.get("sub_period") or {}
    if not rule or "source_step" not in entry:
        return None
    if int(rule.get("dayofweek", -1)) != int(weekday):
        return None
    return float(entry["source_step"])


def _real_prints(src: sqlite3.Connection, sid: str, upto: datetime) -> pd.Series:
    """Real observations of `sid` knowable at `upto` — the weeks a straddling first month
    already has in the world and must keep."""
    rows = src.execute(
        "SELECT event_time, value FROM fred_obs WHERE sid=? AND knowledge_time<=?"
        " AND value IS NOT NULL ORDER BY event_time", (sid, upto.isoformat())).fetchall()
    if not rows:
        return pd.Series(dtype=float)
    ser = pd.Series({pd.Timestamp(r[0]): float(r[1]) for r in rows})
    return ser[~ser.index.duplicated(keep="last")].sort_index()


# ── the published forecast a model reads as an input ─────────────────────────
@dataclass(frozen=True)
class Nowcast:
    """A published forecast OF a generated column, which a model reads as its anchor.

    Deliberately not a `Sink`. A sink is where a generated column is WRITTEN; this is a
    second series that has to be INVENTED alongside it, because the thing the model actually
    conditions on was never in the panel. `gdp.predict` does not read A191RL1Q225SBEA to
    price the current quarter at all — it reads the latest GDPNow vintage and treats the FRED
    series only as the label its error is measured against. A world with a generated GDP path
    and an empty `nowcast_vintages` therefore prices nothing: every event raises "no GDPNow
    vintage visible" and the build reports zero events, which reads as a modelling failure.

    This is the first of these in the project and it may stay the only one. The two other
    forecast tables — `cleveland_nowcast` and `preds` — are not the same shape: Cleveland's
    inflation nowcast is read by `cpi` as one input among several and the panel generates the
    series it forecasts, while `preds` holds this repo's OWN predictions and a world is
    supposed to regenerate those, not inherit them.
    """
    source: str          # nowcast_vintages.source, e.g. 'GDPNow'
    target: str          # nowcast_vintages.target, e.g. 'KXGDP'
    column: str          # the panel column it forecasts
    k_donor: int = 8     # neighbours drawn from, on |truth| — see `synth_nowcast`

    def key(self, period: pd.Timestamp) -> str:
        """The table's `event_time` for a panel period, in the ingest's own convention.

        `ingest/nowcast._quarter_of` writes the REAL rows and this writes the synthetic ones.
        The two must agree exactly, or a world's synthetic path and the real history it is
        spliced onto key the same quarter differently and `predict` finds neither. Pinned
        against that function in the tests rather than shared with it: that one parses a FRED
        `event_time` string, this takes a panel period, and collapsing them would make the
        panel side inherit the ingest side's parsing.
        """
        ts = pd.Timestamp(period)
        return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


NOWCASTS: dict[str, Nowcast] = {
    "gdp_quarterly": Nowcast("GDPNow", "KXGDP", "gdp"),
}


def nowcast_donors(src: sqlite3.Connection, nc: Nowcast, sid: str,
                   upto: datetime) -> list[tuple[float, list[tuple[float, float]]]]:
    """Real `(truth, [(days before release, error)])` blocks for `nc`, PIT at `upto`.

    A BLOCK per reference period, not a bag of errors. A nowcast path tightens as the release
    approaches — GDPNow's mean |error| runs about 2pp three months out and 0.89pp at the last
    vintage — and it is serially dependent within the quarter, because consecutive vintages
    differ only by the data released between them. Drawing vintages independently would
    manufacture a forecast that jitters from one reading to the next and converges on nothing,
    which is neither of those two facts.

    Errors, not levels, so the block can be transplanted onto a synthetic truth. Which is the
    construction §4f licensed by TESTING the dependence rather than assuming it away — see
    `synth_nowcast` for what that test does and does not cover.
    """
    labels: dict[str, tuple[float, pd.Timestamp]] = {}
    for r in src.execute(
            "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs WHERE sid=?"
            " AND knowledge_time<=? AND value IS NOT NULL GROUP BY event_time",
            (sid, upto.isoformat())).fetchall():
        labels[nc.key(pd.Timestamp(r["event_time"]))] = (float(r["value"]),
                                                         pd.Timestamp(r["kt"]))
    out = []
    for (period,) in src.execute(
            "SELECT DISTINCT event_time FROM nowcast_vintages WHERE source=? AND target=?"
            " AND knowledge_time<=? ORDER BY event_time",
            (nc.source, nc.target, upto.isoformat())).fetchall():
        if period not in labels:
            continue                      # forecast of a quarter whose print is not out yet
        y, rel = labels[period]
        seq = [((pd.Timestamp(r["knowledge_time"]) - rel).total_seconds() / 86400.0,
                float(r["value"]) - y)
               for r in src.execute(
                   "SELECT value, knowledge_time FROM nowcast_vintages WHERE source=?"
                   " AND target=? AND event_time=? AND knowledge_time<?"
                   " ORDER BY knowledge_time",
                   (nc.source, nc.target, period, rel.isoformat())).fetchall()
               if r["value"] is not None]
        if seq:
            out.append((y, seq))
    return out


def synth_nowcast(donors: list[tuple[float, list[tuple[float, float]]]],
                  truths: pd.Series, releases: dict, rng, nc: Nowcast,
                  floor: datetime, lead: timedelta = CLOSE_LEAD,
                  ) -> dict[str, list[tuple[datetime, float]]]:
    """Transplant a real error block onto each generated truth: nowcast = truth + eps.

    **Why the donor is drawn on |truth| rather than uniformly.** §4f tested the GDPNow error
    for state-dependence at the FINAL pre-release vintage and found none worth modelling
    (b = 0.95 +/- 0.03, corr(|err|, nowcast) = -0.163), which licenses an independent draw.
    Re-measured here over the whole block, that conclusion holds only at the short horizon:
    corr(|final err|, |truth|) is +0.331 over all 41 donor quarters and -0.222 excluding
    2020 — sign-flipping, i.e. not measurable — but at 45 days out it is **+0.624**, and
    dropping 2020 takes it to +0.161. So the long end of the path IS state-dependent and it
    is 2020 that says so. Transplanting 2020Q3's block (GDPNow was 21pp low three months
    before a +33.1% quarter) onto a synthetic +2.5% quarter would write a +23% nowcast into
    a world, which is not a fat tail — it is a reading that never happened at that state.

    A nearest-neighbour draw on |truth| respects the +0.624 without inventing a functional
    form for it, and it is the same device `bookdonor.draw` already uses for book shapes.
    Because 37 of the 41 donors sit between |truth| 0 and 6, the k nearest of an ordinary
    quarter are very nearly a uniform sample of the ordinary quarters, so this costs almost
    nothing where the correlation is absent and binds only where it is not.

    **Why each path is clipped to start after the PREVIOUS release.** Not a de-collision
    hack, though it is also that. It is how the forecast is actually produced: GDPNow begins
    running on a quarter once the previous quarter's advance estimate is out, and the real
    windows show it exactly — 2024Q4 runs to 2025-01-29, 2025Q1 starts 2025-01-31. Clipping
    to that boundary reproduces the real generating process, and as a by-product no two
    generated periods can collide on a `knowledge_time`, which matters because
    `worlds.write_nowcast`'s primary key does not include the period.
    """
    if not donors:
        raise ValueError(
            f"synth_nowcast: no real {nc.source}/{nc.target} error blocks are visible at the "
            "splice. A world would carry an empty nowcast table and every event would raise "
            "the model's own 'no vintage visible', which reports as zero events generated")
    ys = np.abs(np.array([d[0] for d in donors], dtype=float))
    k = min(int(nc.k_donor), len(donors))
    out: dict[str, list[tuple[datetime, float]]] = {}
    lo = pd.Timestamp(floor)
    for period, y in truths.items():
        rel = pd.Timestamp(releases[period])
        near = np.argsort(np.abs(ys - abs(float(y))), kind="stable")[:k]
        _, seq = donors[int(near[rng.integers(k)])]
        rows = [(rel + timedelta(days=dt), float(y) + err) for dt, err in seq]
        rows = [(kt, v) for kt, v in rows if lo < kt < rel - lead]
        if rows:
            out[nc.key(period)] = [(kt.to_pydatetime(), v) for kt, v in rows]
        lo = max(lo, rel)
    return out


# ── one built world ──────────────────────────────────────────────────────────
@dataclass
class SynthEvent:
    """One scored synthetic event: enough to re-score it under any parameter set."""
    series: str
    path: int
    period: str
    key: str
    close: datetime
    outcome: float
    z_y: float
    donor: str
    world: str


@dataclass
class BuildResult:
    series: str
    cutoff: datetime
    splice: datetime
    anchor: pd.Timestamp
    worlds: list[Path]
    events: list[SynthEvent]
    coverage: dict
    meta: dict = field(default_factory=dict)

    @property
    def n_synth(self) -> int:
        return len(self.events)


def _check_settle_grid_nests(st: Settle, spec, pdata: P.PanelData, say) -> float | None:
    """`level_step` rounds a level that `level_paths` has ALREADY put on the print grid.

    Two roundings, applied one after the other, from two different sources: the measured
    publication grid (`panel.measure_lattice`, carried in the generator's scaler) and the
    Kalshi ladder's strike spacing (`round_rule`). Nothing has ever required them to agree,
    and they are different quantities — they coincide for WTI because a cent is both the tick
    and the print, and for KXPAYROLLS only after the `/scale` divide. KXJOBLESSCLAIMS is the
    case where they visibly differ: strikes every 250, ICSA printing only in multiples of
    1000 (exact GCD over 7783 observations, zero exceptions).

    Measured 2026-08-28 across all eleven `SETTLES` entries, the second rounding is a no-op
    on every one: six identical, five `None`, and claims' measured grid four times COARSER
    than its `level_step` and therefore already on it. So this guard costs nothing today.
    It exists because the two ways it can stop being a no-op are both silent:

      * `level_step` coarser than the measured grid — writes levels at a resolution the real
        series never uses, and for a `strict_gt=False` ladder like KXJOBLESSCLAIMS that
        manufactures exact strike ties, every one of which settles YES;
      * the two not nested at all — the second rounding moves an on-grid level OFF the grid,
        which is defect A reintroduced downstream of the fix for it.

    A changed `round_rule`, a re-specified panel column or a re-measured lattice can each
    trigger either. Raising is the right response rather than warning: a world built on a
    grid the series cannot print is not a degraded world, it is a wrong one, and `verify_settle`
    downstream checks settlement values against real outcomes — not the grid they live on.
    """
    ent = dict(pdata.lattice or {}).get(st.column)
    step_settle = st.level_step(spec.round_rule)
    if step_settle is None or not ent:
        return step_settle
    meas = float(ent["step"])
    ratio = meas / step_settle
    if abs(ratio - round(ratio)) > 1e-9 or round(ratio) < 1:
        raise ValueError(
            f"{spec.ticker}: the settlement rounding grid and the measured print grid do "
            f"not nest — level_step={step_settle:g} (round_rule={spec.round_rule:g}, "
            f"scale={st.scale:g}) against a measured {st.column} grid of {meas:g}. "
            "Rounding onto the first would move levels off the second. Fix whichever is "
            "wrong; do not widen this check.")
    if round(ratio) > 1:
        say(f"  settle grid: {st.column} prints on {meas:g}, {round(ratio)}x coarser than "
            f"the {step_settle:g} ladder step — second rounding is a no-op")
    return step_settle


def build(src: sqlite3.Connection, series: str, cutoff: datetime, *,
          donors: list[B.Donor], out_dir: Path | str, n_paths: int = 8,
          seed: int = 0, epochs: int = 1500, k_local: int = 120, k_draw: int = 10,
          log=None) -> BuildResult:
    """Generate `n_paths` synthetic worlds for `series` as of `cutoff` and score every event.

    `src` is a snapshot, never the live db — `worlds.snapshot` exists for that. Nothing here
    writes to `src`, but the models are handed the WORLD connection and a model that ever
    learns to write would write into a copy.
    """
    say = log or (lambda *_a, **_k: None)
    if series not in SETTLES:
        raise ValueError(
            f"build: {series!r} has no generated settlement column. Generatable: "
            f"{sorted(SETTLES)} (see the module docstring for why KXAAAGASW is not one)")
    st = SETTLES[series]
    panel_name, settle_col = st.panel, st.column
    spec = REGISTRY[series]
    # `_token` names a weekly market for the day it CLOSES and a monthly one for its
    # REFERENCE period, and it reads that off the panel. The panel is chosen by `SETTLES`,
    # the cadence by the registry, and nothing else forces the two to agree — so they are
    # checked here rather than assumed. A mismatch produces tokens no `quotable_events` will
    # ever match, which surfaces as "0 events generated" and looks like a modelling failure.
    cad = cadence(P.PANELS[panel_name]).registry_cadence
    if cad != spec.cadence:
        raise ValueError(
            f"build: {series} settles off panel {panel_name!r} ({P.PANELS[panel_name].freq}"
            f" = {cad}) but the registry calls it {spec.cadence!r} — the event token "
            "convention is derived from the panel and would name every generated event wrong")
    sinks = _sinks(panel_name)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdata = P.build(src, panel_name, cutoff)
    psp = pdata.spec
    anchor = pdata.inc.index[-1]              # last OBSERVATION — see the module docstring
    c_raw = P.condition_row(pdata.levels, pdata.inc, psp, anchor)
    anchor_levels = pdata.levels.loc[anchor]
    say(f"{series}: panel {panel_name} @ {cutoff.date()}  anchors={len(pdata.anchors)} "
        f"n_eff_hint={pdata.n_eff_hint:.1f}  anchor={anchor.date()}")

    # The splice is where real history must stop. Take the LATEST knowledge time among the
    # anchor prints of the generated columns: every one of them was genuinely knowable then,
    # and anything the generated paths overlap is deleted by `write_fred`/`write_fut`.
    clocks = {col: clock(src, psp, sk) for col, sk in sinks.items()}
    splice = max(knowable_at(psp, ck, anchor) for ck in clocks.values())
    say(f"  splice {splice.isoformat()}")

    cfg = G.GenConfig(panel=panel_name, epochs=epochs, seed=seed + 7)
    gen = G.Generator.fit_local(pdata, cfg, c_raw, k=k_local)
    say(f"  generator {gen.meta}")
    paths = gen.level_paths(c_raw, anchor_levels, n_paths, seed=seed + 3)
    # Checked once, before any world is written, and the checked value is the one used —
    # a guard that recomputes what it guards can drift away from it.
    settle_step = _check_settle_grid_nests(st, spec, pdata, say)
    # The panel's own offset, not a hardcoded week: `_MONTHLY` steps MS and `_WEEKLY` W-SAT,
    # and generating a monthly path on a weekly calendar would date every print wrong. Shared
    # with `quantise_levels`, which since PR-17 picks each period's lattice off these same
    # stamps — two copies of the derivation could date a level under a month it was not
    # quantised for.
    fwd = P.forward_periods(psp, anchor, psp.horizon)
    col_ix = {c.name: j for j, c in enumerate(psp.gen_columns)}
    # The real history the settlement transform reads BEFORE the generated path starts. For
    # the first generated period that lookback is real data — dropping it would silently
    # start the synthetic sample one period late (twelve, for a YoY series).
    real_tail = pdata.levels[settle_col].loc[:anchor].tail(st.lookback)

    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    gates = ps.wf_gates()
    rng = np.random.default_rng(seed + 11)
    sigmas = {c: _sigma_daily(src, s.name, splice)
              for c, s in sinks.items() if s.kind == "fut"}
    # A monthly panel carries a weekly source as a monthly MEAN (`Column.agg == "mean"`).
    # Written back as one observation on the first of the month it would read as a series
    # that had gone quiet, so those columns are disaggregated — see `_sub_monthly`.
    cols = {c.name: c for c in psp.gen_columns}
    submonthly = {c: (_fred_weekday(src, sk.name), _sigma_within(src, sk.name, splice),
                      _real_prints(src, sk.name, splice),
                      _sub_print_grid(pdata, c, _fred_weekday(src, sk.name)))
                  for c, sk in sinks.items()
                  if sk.kind == "fred" and cadence(psp).expander == "sub_monthly"
                  and cols[c].agg == "mean"}
    if submonthly:
        say("  sub-monthly sinks: " + ", ".join(
            f"{c} -> {sinks[c].name} on weekday {w}, within-month sd {sd:.4f}, "
            + (f"print grid {g:g}" if g else "NO print grid (continuous weekly values)")
            for c, (w, sd, _, g) in submonthly.items()))
    # The donor blocks are PIT at the splice and independent of the path, so they are read
    # once. `nc_rel` likewise: the release calendar is a property of the panel's clock.
    ncst = NOWCASTS.get(panel_name)
    nc_donors, nc_rel = [], {}
    if ncst is not None:
        if ncst.column not in sinks:
            raise ValueError(
                f"build: panel {panel_name!r} has a nowcast of column {ncst.column!r}, which "
                f"is not one of its sinks {sorted(sinks)} — the error blocks are measured "
                "against the FRED series the column is written to, so there is nothing to "
                "measure them against")
        nc_donors = nowcast_donors(src, ncst, sinks[ncst.column].name, splice)
        nc_rel = {t: knowable_at(psp, clocks[ncst.column], t) for t in fwd}
        say(f"  nowcast {ncst.source}/{ncst.target} of {ncst.column}: "
            f"{len(nc_donors)} real error blocks visible at the splice")

    world_paths, events, z_ys = [], [], []
    for i in range(n_paths):
        wp = out_dir / f"world_{series}_{i:02d}.db"
        dst = W.materialize(src, wp, splice)
        W.clear_series(dst, series, after=splice)

        outcomes, written = None, {}
        for col, sk in sinks.items():
            lv = pd.Series(paths[i, :, col_ix[col]], index=fwd)
            if col == settle_col:
                if settle_step:
                    lv = (lv / settle_step).round() * settle_step
                # Derived from the level that is about to be written, so the number the
                # world holds and the number the event settles on are the same object.
                outcomes = outcome_path(pd.concat([real_tail, lv]), st).reindex(fwd)
            # After the quantisation, so a nowcast is a forecast of the number the world will
            # actually hold rather than of the unrounded draw behind it.
            written[col] = lv
            ck = clocks[col]
            idx = pd.DatetimeIndex([observation_date(psp, ck, t) for t in lv.index])
            if col in submonthly:
                weekday, sigma_w, real, wgrid = submonthly[col]
                wk = _sub_monthly(lv, weekday, sigma_w, rng, fixed=real, grid=wgrid,
                                  log=say)
                # Only weeks the splice has not already made real. `write_fred` deletes
                # from its first row onward, so writing a pre-splice week would rewrite
                # history that was genuinely knowable — not a leak, but a world claiming a
                # past that did not happen, and `_sub_monthly` has already held those weeks
                # fixed when solving for the month's mean.
                wk = wk[wk.index > pd.Timestamp(splice.date())]
                W.write_fred(dst, sk.name, wk, lag_days=ck.lag_days, hour=ck.hour)
            elif sk.kind == "fred":
                W.write_fred(dst, sk.name, lv.set_axis(idx),
                             lag_days=ck.lag_days, hour=ck.hour)
            else:
                # anchor the bridge on the real last close, then expand to business days
                wk = pd.concat([pd.Series({observation_date(psp, ck, anchor):
                                           float(anchor_levels[col])}), lv.set_axis(idx)])
                W.write_fut(dst, sk.name, _daily_bridge(wk, sigmas[col], rng),
                            hour=ck.hour)

        if ncst is not None:
            npaths = synth_nowcast(nc_donors, written[ncst.column], nc_rel, rng, ncst,
                                   floor=splice)
            n_nc = W.write_nowcast(dst, ncst.target, npaths, source=ncst.source)
            if not n_nc:
                raise ValueError(
                    f"build: {series} generated no {ncst.source} vintages for path {i}. Every "
                    "event would then hit the model's own 'no vintage visible' refusal and "
                    "the build would report zero events, which reads as a modelling failure")

        n_ok = 0
        for w in range(1, psp.horizon):
            per = fwd[w]
            close = knowable_at(psp, clocks[settle_col], per) - CLOSE_LEAD
            tok = _token(psp, close, per)
            key = kalshi_period_to_key(tok)
            y = outcomes.iloc[w]
            if not np.isfinite(y):
                continue
            ev = _one_event(dst, fn, series, spec, tok, key, close, float(y), donors, rng,
                            gates, k_draw)
            if ev is None:
                continue
            ev.path, ev.world = i, str(wp)
            events.append(ev)
            z_ys.append(ev.z_y)
            n_ok += 1
        say(f"  path {i}: {n_ok}/{psp.horizon - 1} events, "
            f"quotable={len(ps.quotable_events(dst, series))}")
        dst.close()
        world_paths.append(wp)

    cov = B.coverage(donors, z_ys)
    return BuildResult(series=series, cutoff=cutoff, splice=splice, anchor=anchor,
                       worlds=world_paths, events=events, coverage=cov,
                       meta={"panel": panel_name, "generator": gen.meta,
                             "n_paths": n_paths, "sigma_daily": sigmas,
                             "clocks": {c: vars(k) for c, k in clocks.items()}})


def _token(spec: P.PanelSpec, close: datetime, period: pd.Timestamp) -> str:
    """The Kalshi event token for a generated period, in that series' own convention.

    Read off the real db rather than invented. A monthly market is named for the REFERENCE
    month — KXCPI-26JUL is July's CPI, released in August — and the models take `period` as
    exactly that (`cpi._gas_effect` does `pd.Period(ref_month)`). A weekly market is named
    for the day it CLOSES: KXWTIW-26MAY2914 settles on the 29 May session, and
    KXJOBLESSCLAIMS-26JUL30 is the 30 July release of the week ending 25 July, which is why
    `claims.predict` recovers its target week as `period - 5 days`.

    Both fall out of the same two facts, so neither is a table: the token names either the
    reference period or the close date, and the close date is itself derived from when the
    number becomes knowable.

    Which of the two it is comes from `Cadence.token` rather than from the frequency, and
    KXGDP is why: it is quarterly and named for its release date (KXGDP-27JAN28), so the
    frequency and the naming convention genuinely disagree. See `Cadence` (#212).
    """
    if cadence(spec).token == "close_date":
        return close.strftime("%y%b%d").upper()
    return period.strftime("%y%b").upper()


def _one_event(dst, fn, series: str, spec, tok: str, key: str, close: datetime,
               y: float, donors: list[B.Donor], rng, gates,
               k_draw: int) -> SynthEvent | None:
    """Run the incumbent across the replay days, transplant a book, write and check it.

    Returns None when the model cannot produce a usable distribution on any replay day —
    which happens and is not an error: at the start of a generated path there may not yet be
    enough synthetic history for the model's own minimum window.
    """
    per_day, p0 = [], None
    for d in ps.entry_days(close, gates):
        try:
            pred = fn(dst, d, key, series=series, params=None)
        except RuntimeError:
            continue                     # model's own "history too short" — a real refusal
        pm, psd, _ = B.pinned_moments(grid_pmf(pred.dist, spec.round_rule),
                                      spec.round_rule)
        if not psd:
            continue
        per_day.append((d, pm, psd))
        if p0 is None:
            p0 = (pm, psd)
    if not per_day:
        return None
    z_y = (y - p0[0]) / p0[1]
    donor = B.draw(donors, z_y, rng, k=k_draw)
    d0 = donor.day_at((close - per_day[0][0]).total_seconds() / 86400.0)
    m_mean = p0[0] + d0.z_m * p0[1]
    m_sd = max(d0.r * p0[1], spec.round_rule / 4.0)
    legs = B.build_ladder(B.draw_ladder(donors, series, rng), series, tok, m_mean, m_sd)
    W.write_event(dst, W.EventPlan(series=series, period=tok, legs=legs,
                                   close_time=close, outcome=y,
                                   book=B.quote(donor, legs, series, per_day, close)))
    return SynthEvent(series=series, path=-1, period=tok, key=key, close=close,
                      outcome=y, z_y=z_y, donor=f"{donor.series}/{donor.tok}", world="")


# ── the settlement transform, checked against real settled events ────────────
def verify_settle(src: sqlite3.Connection, series: str, now: datetime,
                  log=None) -> dict:
    """Recompute every real settled outcome of `series` from the real panel, and require it
    to be consistent with what the legs actually paid.

    This is the only claim in `SETTLES` that cannot be checked by reading the code, and it
    is the one most likely to be wrong: KXPAYROLLS settles in JOBS while PAYEMS is carried
    in thousands, KXU3 settles on the LEVEL while `unrate` enters the panel as a difference,
    KXCPI settles on a month-over-month percent computed from an index. Each of those is a
    sentence someone could write confidently and get backwards, and the resulting world
    would be perfectly self-consistent while scoring a market nobody trades.

    The test is INTERVAL MEMBERSHIP, not equality. A settled event records a yes/no pattern,
    from which `worlds.implied_interval` recovers the tightest range the true value can lie
    in; the stored "outcome" is that range's midpoint and is therefore up to half a ladder
    bucket from the truth by construction. Asking the recomputed value to sit inside the
    range asks exactly as much as the data can answer, and a units error — a factor of a
    thousand, a level where a change belongs — misses by orders of magnitude, not by a
    bucket.

    **Why `diff` reads the panel's increment rather than differencing its levels.** For
    PAYEMS the two are different numbers: a difference of the first-print level chain mixes
    the vintage of month t with the vintage of t-1 and so folds the revision to t-1 into the
    change, which is not what the market settles on. `panel._payems_printed` exists for that
    reason, and it is what `inc` carries. In a SYNTHETIC world the two coincide exactly —
    `integrate` builds the level from the very increments being generated and there are no
    revisions — which is why `build` can take the same number off the level path. That
    identity is pinned separately in the tests; here the real-data path uses the real-data
    definition.
    """
    say = log or (lambda *_a, **_k: None)
    if series not in SETTLES:
        raise ValueError(f"verify_settle: {series!r} is not a generated series")
    st = SETTLES[series]
    pspec = P.PANELS[st.panel]
    col = next(c for c in pspec.columns if c.name == st.column)
    if st.how == "diff" and col.scale != st.scale:
        raise ValueError(
            f"verify_settle: {series} settles on {st.column} scaled by {st.scale} but the "
            f"panel column carries scale {col.scale}. For a `diff` series the two ARE the "
            "same conversion — the panel increment is the settlement value — so a "
            "disagreement means one of them is wrong")
    pdata = P.build(src, st.panel, now)
    got = pdata.inc[st.column] if st.how == "diff" \
        else outcome_path(pdata.levels[st.column], st)
    # Only the close-date inverse needs a clock; a reference-period token names its own
    # bucket outright. Measuring one anyway would make this refuse a panel whose sink map is
    # still being built, which is a different question from whether the transform is right.
    ck = clock(src, pspec, SINKS[st.panel][st.column]) \
        if cadence(pspec).token == "close_date" else None

    periods = [r[0] for r in src.execute(
        "SELECT DISTINCT s.period FROM settlements s JOIN contracts c ON c.ticker=s.ticker"
        " WHERE c.series=? AND s.result IN ('yes','no') ORDER BY s.period", (series,))]
    checked, bad, skipped = [], [], []
    for period in periods:
        key = kalshi_period_to_key(period)
        if not key:
            skipped.append((period, "unparseable token"))
            continue
        try:
            plan = W.read_event(src, series, period)
            lo, hi = W.implied_interval(plan.legs, series)
        except ValueError as e:
            skipped.append((period, str(e)))
            continue
        ts = _panel_period(pspec, ck, got.index, key, plan.close_time)
        if ts is None or not np.isfinite(got.get(ts, np.nan)):
            skipped.append((period, f"no panel observation at {ts}"))
            continue
        v = float(got.loc[ts])
        # Compare on the ladder's own grid. The statistical agencies publish a rounded
        # number — CPI YoY for September 2024 is "2.4%", not the 2.4265 the index ratio
        # gives — and it is the rounded one that settles. `eps` is a float-encoding slack,
        # not a tolerance: Kalshi writes the 4.1 strike of KXU3 as 4.099999.
        step = REGISTRY[series].round_rule
        eps = step * 1e-3
        v_grid = round(v / step) * step
        row = {"period": period, "panel_period": str(ts.date()), "computed": v,
               "on_grid": v_grid, "lo": lo, "hi": hi, "stored": plan.outcome}
        (checked if lo - eps <= v_grid <= hi + eps else bad).append(row)
    out = {"series": series, "how": st.how, "column": st.column, "scale": st.scale,
           "n_ok": len(checked), "n_bad": len(bad), "n_skipped": len(skipped),
           "bad": bad[:10], "skipped": skipped[:10]}
    say(f"  {series}: {len(checked)} consistent, {len(bad)} inconsistent, "
        f"{len(skipped)} unusable")
    return out


def _panel_period(spec: P.PanelSpec, ck: Clock, index: pd.DatetimeIndex, key: str,
                  close: datetime | None) -> pd.Timestamp | None:
    """The panel bucket a real event refers to — the inverse of `_token`, by inversion.

    A reference-period token is direct: the token IS the bucket, and the panel labels buckets
    by their first day, so flooring the parsed date onto `Cadence.period_floor` recovers it.
    A close-date token is not, and the two weekly conventions disagree in opposite directions
    — KXJOBLESSCLAIMS closes five days AFTER the Saturday its number is dated, KXWTIW closes
    the day BEFORE the Saturday of the week its session belongs to. Rather than encode both
    offsets a second time and risk them drifting from the ones `build` writes with, the
    bucket is found by running `knowable_at` forward over the panel's own index and taking
    the period whose publication lands closest to the real close. A wrong answer here would
    have to be a whole bucket wrong, which the surrounding consistency check would catch.

    The branch is `Cadence.token`, not the frequency (#212), so a quarterly close-date series
    such as KXGDP takes the search rather than falling through to a monthly label it does not
    have. The search's own window and tolerance are read off the panel's index spacing rather
    than tabulated as a week, for the same reason: at ±14 days a quarterly panel would find
    no candidate at all, and the number that is actually meant here is "two periods" and
    "half a period". On a weekly index the median spacing is exactly 7 days, so both come out
    at the values they were written as and nothing about the ten live series moves.
    """
    cad = cadence(spec)
    if cad.token != "close_date":
        return pd.Timestamp(key).normalize().to_period(cad.period_floor).start_time
    if close is None or len(index) == 0:
        return None
    step = (float(np.median(np.diff(index.asi8))) / 1e9 if len(index) > 1
            else 7.0 * 86400.0)
    cand = index[(index >= pd.Timestamp(close.date()) - pd.Timedelta(seconds=2 * step))
                 & (index <= pd.Timestamp(close.date()) + pd.Timedelta(seconds=2 * step))]
    if len(cand) == 0:
        return None
    gaps = [abs((knowable_at(spec, ck, t) - close).total_seconds()) for t in cand]
    best = int(np.argmin(gaps))
    # Nearest is not the same as right. A panel built today ends where its shortest column
    # ends, so the bucket a recent event settles on may simply not be there — and without
    # this the search silently snaps to the week before and reports a $4 discrepancy that is
    # entirely the checker's own doing. Half a period is the widest a correct match can miss
    # by, since publication lags are constant within a series.
    return cand[best] if gaps[best] < 0.5 * step else None


# ── scoring a grid on already-built worlds ───────────────────────────────────
def _score_world(wp: str, evs: list[SynthEvent],
                 grid: list[dict]) -> tuple[list[SynthEvent], list[list[float]]]:
    """One world's rows. Module-level and self-contained so it can be pickled to a pool."""
    conn = sqlite3.connect(wp)
    conn.row_factory = sqlite3.Row
    kept: list[SynthEvent] = []
    mat: list[list[float]] = []
    try:
        for e in evs:
            row = []
            for p in grid:
                r = ps.event_pnl(conn, e.series, e.period, e.key, e.close,
                                 params=(p or None))
                if r is None:
                    row = None
                    break
                row.append(float(r["hybrid"]))
            if row is None:
                continue
            kept.append(e)
            mat.append(row)
    finally:
        conn.close()
    return kept, mat


def score_matrix(events: list[SynthEvent], grid: list[dict],
                 log=None, workers: int | None = None
                 ) -> tuple[list[SynthEvent], list[list[float]]]:
    """(kept events, [[hybrid PnL per set] per event]) — `pnl_score.score_matrix`'s contract.

    Deliberately the same shape and the same keep-rule as the real-sample scorer, because
    the whole of S5 and S6 is a comparison between the two and a difference in how the two
    matrices are formed would show up as a difference in what they measure. In particular an
    event is kept only when EVERY set replays on it: a partial row compares candidates on
    different samples, which is the exact bias the paired test exists to remove.

    Writing worlds to disk rather than scoring inline is what makes this possible — a grid
    of K candidates is K passes over the SAME worlds, so the synthetic sample is held fixed
    across the grid exactly as the real one is. Regenerating per candidate would be scoring
    parameter sets on different data and calling the difference skill.

    **Why this is parallel, and why only here.** This loop is the weekly job's entire cost.
    `event_pnl` re-runs the forecasting model per candidate — `params` changes the model, so
    the tape cannot be loaded once and reused across the grid, and the 215 ms a pair costs is
    model time rather than a scan that could be indexed away. Measured over the seven monthly
    markets that is ~380 min of one pinned core (KXPAYROLLS alone 88 x 222 = 19,536 pairs),
    which is too long to hold `refresh`'s flock. Worlds are independent files, so a pool over
    them is the one speed-up available that changes NOTHING about what is computed: same
    events, same grid, same `event_pnl`. It is deliberately not a pool over the grid, which
    would reopen each world K times, nor over events, which would fight for one file's page
    cache. Results are slotted by world index and concatenated in the same sorted order the
    serial path walks, because `mat[i]` is paired to `kept[i]` and every use of this matrix
    downstream is a paired comparison — `as_completed` returns in completion order and
    extending as futures land would silently permute the sample. Serial below two worlds:
    process startup would dominate, and a subprocess drops any `event_pnl` fake a caller
    installed, turning a fast test into a real model run.

    **Callers must be import-safe.** macOS spawns rather than forks, so each worker
    re-imports the parent's `__main__`; a script without an `if __name__ == "__main__"`
    guard re-runs itself in every child and the pool dies with `BrokenProcessPool`. The
    production entry points entered via `python -m` are guarded, but an ad-hoc script is
    not until it is written that way. Fork would sidestep it and is the wrong trade: `one`
    generates with torch in this same process, and forking an interpreter that already has
    torch's thread pool up is how you get a deadlock instead of an exception.

    Verified equal to the serial path on real worlds with the real model, not just against
    fakes: KXPCECORE's 26 events x 5 candidates over 2 worlds return identical `kept` order
    and an identical matrix, 12.9 s serial against 7.8 s parallel.
    """
    by_world: dict[str, list[SynthEvent]] = {}
    for e in events:
        by_world.setdefault(e.world, []).append(e)
    items = sorted(by_world.items())
    if workers is None:
        workers = min(len(items), max(1, (os.cpu_count() or 4) - 4))
    kept: list[SynthEvent] = []
    mat: list[list[float]] = []
    if workers > 1 and len(items) > 1:
        # Results are collected into a slot per world and concatenated in the SAME
        # sorted order the serial path walks, so the pool changes the wall clock and
        # nothing else — `test_parallel_scoring_matches_serial_exactly` pins that.
        with cf.ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_score_world, wp, evs, grid): i
                    for i, (wp, evs) in enumerate(items)}
            slots: list[tuple[list, list] | None] = [None] * len(items)
            for f in cf.as_completed(futs):
                slots[futs[f]] = f.result()
        for k, m in slots:
            kept.extend(k)
            mat.extend(m)
    else:
        for wp, evs in items:
            k, m = _score_world(wp, evs, grid)
            kept.extend(k)
            mat.extend(m)
    if log:
        log(f"  {events[0].series if events else '?'}: synthetic-scored {len(kept)}/"
            f"{len(events)} events x {len(grid)} sets")
    return kept, mat
