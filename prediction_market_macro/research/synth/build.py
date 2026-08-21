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

import importlib
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


def _weekly(spec: P.PanelSpec) -> bool:
    return spec.freq.upper().startswith("W")


def clock(src: sqlite3.Connection, spec: P.PanelSpec, sk: Sink) -> Clock:
    """Measure `sk`'s publication clock. Nothing here is tabulated."""
    if sk.kind == "fred":
        lag, hour = W.publication_lag(src, sk.name)
        return Clock("fred", lag, hour,
                     _fred_weekday(src, sk.name) if _weekly(spec) else None)
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
    # The panel's own offset, not a hardcoded week: `_MONTHLY` steps MS and `_WEEKLY` W-SAT,
    # and generating a monthly path on a weekly calendar would date every print wrong.
    fwd = pd.date_range(anchor, periods=psp.horizon + 1, freq=psp.freq)[1:]
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

    world_paths, events, z_ys = [], [], []
    for i in range(n_paths):
        wp = out_dir / f"world_{series}_{i:02d}.db"
        dst = W.materialize(src, wp, splice)
        W.clear_series(dst, series, after=splice)

        outcomes = None
        for col, sk in sinks.items():
            lv = pd.Series(paths[i, :, col_ix[col]], index=fwd)
            if col == settle_col:
                step = st.level_step(spec.round_rule)
                if step:
                    lv = (lv / step).round() * step
                # Derived from the level that is about to be written, so the number the
                # world holds and the number the event settles on are the same object.
                outcomes = outcome_path(pd.concat([real_tail, lv]), st).reindex(fwd)
            ck = clocks[col]
            idx = pd.DatetimeIndex([observation_date(psp, ck, t) for t in lv.index])
            if sk.kind == "fred":
                W.write_fred(dst, sk.name, lv.set_axis(idx),
                             lag_days=ck.lag_days, hour=ck.hour)
            else:
                # anchor the bridge on the real last close, then expand to business days
                wk = pd.concat([pd.Series({observation_date(psp, ck, anchor):
                                           float(anchor_levels[col])}), lv.set_axis(idx)])
                W.write_fut(dst, sk.name, _daily_bridge(wk, sigmas[col], rng),
                            hour=ck.hour)

        n_ok = 0
        for w in range(1, psp.horizon):
            per = fwd[w]
            close = knowable_at(psp, clocks[settle_col], per) - CLOSE_LEAD
            tok = _token(spec, close, per)
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

    Both fall out of the same two facts, so neither is a table: the token is the reference
    period monthly and the close date weekly, and the close date is itself derived from when
    the number becomes knowable.
    """
    if _weekly(spec):
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
    # Only the weekly inverse needs a clock; a monthly token names its reference month
    # outright. Measuring one anyway would make this refuse a panel whose sink map is still
    # being built, which is a different question from whether the transform is right.
    ck = clock(src, pspec, SINKS[st.panel][st.column]) if _weekly(pspec) else None

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

    Monthly is direct: the token IS the reference month and the panel labels months by their
    first day. Weekly is not, and the two weekly conventions disagree in opposite directions
    — KXJOBLESSCLAIMS closes five days AFTER the Saturday its number is dated, KXWTIW closes
    the day BEFORE the Saturday of the week its session belongs to. Rather than encode both
    offsets a second time and risk them drifting from the ones `build` writes with, the
    bucket is found by running `knowable_at` forward over the panel's own index and taking
    the period whose publication lands closest to the real close. A wrong answer here would
    have to be a whole bucket wrong, which the surrounding consistency check would catch.
    """
    if not _weekly(spec):
        return pd.Timestamp(key).normalize().replace(day=1)
    if close is None or len(index) == 0:
        return None
    cand = index[(index >= pd.Timestamp(close.date()) - pd.Timedelta(days=14))
                 & (index <= pd.Timestamp(close.date()) + pd.Timedelta(days=14))]
    if len(cand) == 0:
        return None
    gaps = [abs((knowable_at(spec, ck, t) - close).total_seconds()) for t in cand]
    best = int(np.argmin(gaps))
    # Nearest is not the same as right. A panel built today ends where its shortest column
    # ends, so the bucket a recent event settles on may simply not be there — and without
    # this the search silently snaps to the week before and reports a $4 discrepancy that is
    # entirely the checker's own doing. Half a period is the widest a correct match can miss
    # by, since publication lags are constant within a series.
    return cand[best] if gaps[best] < 0.5 * 7 * 86400 else None


# ── scoring a grid on already-built worlds ───────────────────────────────────
def score_matrix(events: list[SynthEvent], grid: list[dict],
                 log=None) -> tuple[list[SynthEvent], list[list[float]]]:
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
    """
    by_world: dict[str, list[SynthEvent]] = {}
    for e in events:
        by_world.setdefault(e.world, []).append(e)
    kept: list[SynthEvent] = []
    mat: list[list[float]] = []
    for wp, evs in sorted(by_world.items()):
        conn = sqlite3.connect(wp)
        conn.row_factory = sqlite3.Row
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
    if log:
        log(f"  {events[0].series if events else '?'}: synthetic-scored {len(kept)}/"
            f"{len(events)} events x {len(grid)} sets")
    return kept, mat
