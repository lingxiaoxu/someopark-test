"""synth/panel.py — the point-in-time macro panel the generator is trained on.

**What a training row is.** A diffusion model generates iid cross-sections; macro data is
a time series. The bridge used here is *anchor + forward path*: one row is

    c  = the macro state at an anchor date T   (levels, recent drift, recent vol, calendar)
    z  = the next H periods of INCREMENTS      (H x d, flattened)

and the generator learns p(z | c). Two properties fall out of that choice and both are
load-bearing:

* **Generation starts from today by construction.** Increments are re-integrated from a
  supplied anchor level (`integrate`), so a path generated at c = "today" begins at
  today's oil price, today's claims level, today's unemployment rate. Modelling levels
  instead would have the generator happily reproduce 1998's $12 crude, which is not the
  environment any parameter set is about to be used in. This is the user requirement that
  the synthetic sample resemble *now* rather than the average of forty years.
* **The lookback the models need is real data, not generated data.** A synthetic world
  splices H generated periods onto the actual history (`worlds.py`), so `payrolls.predict`
  reading 12 prints back, or `u3.predict` reading 60 months back, is reading mostly the
  real series. Only the part that has not happened yet is invented.

**First prints, not latest vintages.** A Kalshi contract settles on the first print, and
the print-anchored models (payrolls, u3, claims) read `fred_first_prints`. Training the
generator on revised history would fit a series nobody ever traded. The consequence,
stated rather than hidden: a synthetic world has no revisions at all (first == latest),
so a model that exploited the revision process would be scored generously here. None of
the current models do; if one ever does, this is the assumption that has to be revisited.

**Overlapping windows.** Anchors step one period at a time, so rows share most of their
history. That inflates the row count without inflating the information: the effective
number of independent draws is about `n_rows / horizon`, and `PanelData.n_eff_hint`
carries that number so nothing downstream can quietly treat 400 windows as 400
observations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np
import pandas as pd

from prediction_market_macro.model.features import FeatureStore

# ── column algebra ───────────────────────────────────────────────────────────
# `transform` is how a level becomes a stationary increment, and it is also the rule
# `integrate` inverts. "diff" and "level" are self-explanatory; "dlog" is the log change,
# used for every strictly-positive series (prices, stocks, claims) so that a generated
# path cannot walk negative no matter how long it runs.
TRANSFORMS = ("diff", "dlog", "level", "pct100")


@dataclass(frozen=True)
class Column:
    """One panel column: where it comes from and how it is made stationary.

    `agg` collapses a source sampled faster than the panel ("mean" for prices and stocks,
    whose monthly average is the economically meaningful summary; "last" for anything the
    models read as a point-in-time level).

    `prints` must match what the CONSUMING model reads, not what feels more correct in the
    abstract. `payrolls`/`u3` are print-anchored and read `fred_first_prints`; `cpi`/`pce`
    read `fred_series` (latest vintage) and must keep doing so here, because BEA rebases
    PCEPILFE every few years and a first-print chain across a rebase is a chain across two
    different index bases — that showed up as an 8.8% monthly "core PCE print" in the
    first build of this panel.

    `scale` is the factor between the increment's units and the level's: PAYEMS is stored
    in thousands of persons and the model works in jobs, so the column carries scale=1000
    and `integrate` divides back out.

    `inc_fn` overrides how the increment is derived when the model computes something a
    plain difference of the stored level cannot reproduce — see `_INC_FNS`.

    `generate=False` makes the column CONTEXT ONLY: it enters the condition vector but not
    the forward path. This is close to free and it matters a great deal. Output dimension
    is what the generator pays for — H*d dims learned from a few hundred overlapping
    windows — while condition dimension is only an input. The models this package feeds
    read two to four series each (`payrolls` reads PAYEMS and ICSA; `energy` reads CL/NG/RB,
    GASREGW and AAA_DAILY; nothing reads the storage series at all), so generating a
    twelve-column world was paying twelve columns of estimation cost to score a two-column
    model. Storage, rates and the rest stay in as context because "resembles the current
    environment" is the entire point of conditioning, and context costs nothing to carry.
    """
    name: str
    source: str          # "fred" | "fut"
    sid: str             # FRED series id or futures root
    prints: str          # "first" | "latest"  (ignored for futures)
    agg: str             # "mean" | "last"
    transform: str
    unit: str
    scale: float = 1.0
    inc_fn: str | None = None
    generate: bool = True

    def __post_init__(self):
        if self.transform not in TRANSFORMS:
            raise ValueError(f"{self.name}: unknown transform {self.transform!r}")
        if self.source not in ("fred", "fut"):
            raise ValueError(f"{self.name}: unknown source {self.source!r}")


@dataclass(frozen=True)
class PanelSpec:
    """A named panel. `horizon` is H, the number of forward periods in a training row.

    `drop_spans` removes ANCHORS whose forward path overlaps the span, while leaving the
    rows in `inc` so conditions computed near the span still see it. It exists for COVID
    and the reasoning is the same one that runs through `payrolls._residual_sigma`: 2020
    is not a draw from the distribution a parameter set chosen today will face, and at
    24 of 394 monthly anchors it would otherwise supply 6% of the synthetic sample. The
    cost is stated rather than hidden — the generator trained this way CANNOT produce a
    pandemic-scale tail, so the synthetic sample understates disaster risk and must never
    be read as a stress test.

    `level_lag` is the trailing window the condition measures each level AGAINST — see
    `condition_row`. It costs `level_lag - VOL_LAG` anchors off the front of the panel.
    """
    name: str
    freq: str            # pandas offset alias: "MS" monthly, "W-SAT" weekly
    horizon: int
    columns: tuple[Column, ...]
    start: str           # first period kept; set by the latest-starting column
    note: str = ""
    drop_spans: tuple[tuple[str, str], ...] = ()
    level_lag: int = 60

    @property
    def gen_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.generate)

    @property
    def d(self) -> int:
        """The GENERATED width. Everything downstream — Z, `integrate`, the synthetic
        world's writable columns — is this wide, not `d_all`."""
        return len(self.gen_columns)

    @property
    def d_all(self) -> int:
        return len(self.columns)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.gen_columns]

    @property
    def all_names(self) -> list[str]:
        return [c.name for c in self.columns]


# The monthly column set. Start is 1990-09 because GASREGW's first observation is
# 1990-08-20 and the gasoline/crude stock series begin 1990-01/1982-08; everything else
# reaches further back (PAYEMS 1939, CPI 1947, UNRATE 1948). Natural gas is deliberately
# absent: NG_STORAGE_WEEKLY only starts 2010 and would cost 20 years of every other
# column. Gas lives in the weekly panel, which is the frequency its market trades at.
_MONTHLY_COLS = (
    Column("payems", "fred", "PAYEMS", "first", "last", "diff", "jobs",
           scale=1000.0, inc_fn="payems_printed"),
    Column("claims", "fred", "ICSA", "latest", "mean", "dlog", "count"),
    Column("unrate", "fred", "UNRATE", "first", "last", "diff", "pct"),
    Column("cpi", "fred", "CPIAUCSL", "latest", "last", "pct100", "%mom"),
    Column("cpi_core", "fred", "CPILFESL", "latest", "last", "pct100", "%mom"),
    Column("pce_core", "fred", "PCEPILFE", "latest", "last", "pct100", "%mom"),
    Column("gas_retail", "fred", "GASREGW", "latest", "mean", "dlog", "$gal"),
    Column("wti", "fred", "DCOILWTICO", "latest", "mean", "dlog", "$bbl"),
    Column("crude_stocks", "fred", "CRUDE_STOCKS_WEEKLY", "latest", "mean", "dlog", "kb"),
    Column("gaso_stocks", "fred", "GASOLINE_STOCKS_WEEKLY", "latest", "mean", "dlog",
           "kb"),
    Column("dgs2", "fred", "DGS2", "latest", "mean", "diff", "pct"),
    Column("dgs10", "fred", "DGS10", "latest", "mean", "diff", "pct"),
)

# The weekly column set. Start 2010-01 is NG_STORAGE_WEEKLY's first observation, and
# natural gas is the point of the weekly panels.
#
# `claims` is "first" HERE and "latest" in `_MONTHLY_COLS`, and the split is not cosmetic:
# it follows the consumer. `claims.predict` reads ICSA through `fred_first_prints` and
# KXJOBLESSCLAIMS settles on the advance print; `payrolls`/`u3` read ICSA through
# `fred_series` as a context feature and never settle on it. Measured on the production db,
# 1042 of 3110 weeks carry a revision (mean |rev| 4,228 claims) and the advance print is the
# noisier series — dlog sd 0.07150 first vs 0.06729 latest. Training the generator on the
# revised chain therefore understated settlement noise by ~6%, which is one measurable piece
# of the §5c finding that the synthetic world is easier to trade than the real one. The
# check that surfaced it: of the 7 weeks where `build.verify_settle` put the computed
# outcome outside the Kalshi interval, all 7 had a revision and 6 land INSIDE the interval
# on the first print.
_WEEKLY_COLS = (
    Column("claims", "fred", "ICSA", "first", "last", "dlog", "count"),
    Column("gas_retail", "fred", "GASREGW", "latest", "last", "dlog", "$gal"),
    Column("wti", "fut", "CL", "latest", "last", "dlog", "$bbl"),
    Column("natgas", "fut", "NG", "latest", "last", "dlog", "$mmbtu"),
    Column("rbob", "fut", "RB", "latest", "last", "dlog", "$gal"),
    Column("crude_stocks", "fred", "CRUDE_STOCKS_WEEKLY", "latest", "last", "dlog", "kb"),
    Column("gaso_stocks", "fred", "GASOLINE_STOCKS_WEEKLY", "latest", "last", "dlog",
           "kb"),
    Column("ng_stocks", "fred", "NG_STORAGE_WEEKLY", "latest", "last", "dlog", "bcf"),
    Column("dgs2", "fred", "DGS2", "latest", "mean", "diff", "pct"),
    Column("dgs10", "fred", "DGS10", "latest", "mean", "diff", "pct"),
)


def _scope(cols: tuple[Column, ...], generate: tuple[str, ...]) -> tuple[Column, ...]:
    """Keep every column as CONTEXT, mark only `generate` as producing a forward path.

    The order of `cols` is preserved, so the condition vector is identical across every
    panel built from the same column set and the panels differ only in what they pay to
    generate.
    """
    want = set(generate)
    unknown = want - {c.name for c in cols}
    if unknown:
        raise ValueError(f"_scope: no such column(s) {sorted(unknown)}")
    return tuple(replace(c, generate=c.name in want) for c in cols)


_MONTHLY = dict(freq="MS", horizon=12, start="1990-09-01",
                drop_spans=(("2020-02-01", "2021-01-01"),),
                # five years: long enough to define "normal", short enough that a regime
                # (the 2021- inflation era) still reads as a deviation
                level_lag=60)
_WEEKLY = dict(freq="W-SAT", horizon=13, start="2010-01-09",
               drop_spans=(("2020-02-01", "2021-01-01"),),
               # two years; the monthly panel's five, in weeks, would eat a quarter of a
               # panel that only starts in 2010
               level_lag=104)

# The wide panels: every column generated. These are the CONTROL arm, not the product.
# Measured on a purged 5-fold, `core_monthly` at 144 output dims from ~27 independent
# draws loses to a block bootstrap of its own history at every conditioning width
# including zero (KS 0.113 unconditional vs 0.067 bootstrap). They are kept because that
# comparison is the evidence for the narrow panels below and has to stay reproducible.
CORE_MONTHLY = PanelSpec(
    name="core_monthly",
    note="ALL monthly columns generated — wide control arm, 12 x 12 = 144 dims",
    columns=_scope(_MONTHLY_COLS, tuple(c.name for c in _MONTHLY_COLS)),
    **_MONTHLY,
)
ENERGY_WEEKLY_WIDE = PanelSpec(
    name="energy_weekly_wide",
    note="ALL weekly columns generated — wide control arm, 10 x 13 = 130 dims",
    columns=_scope(_WEEKLY_COLS, tuple(c.name for c in _WEEKLY_COLS)),
    **_WEEKLY,
)

# The narrow panels, one per model family. Each generates only what its consuming models
# read and carries the rest as context. The audit that fixed these lists, taken from the
# models' own `FeatureStore` calls:
#
#   payrolls  ICSA PAYEMS          claims  ICSA              u3    ICSA UNRATE
#   cpi       CPILFESL GASREGW RB  pce     CPILFESL PCEPILFE bridge GASREGW ICSA
#   energy    AAA_DAILY GASREGW RB + fut CL/NG/RB
#   fed       CPILFESL DFEDTARU DGS2 UNRATE
#
# Nothing reads the crude/gasoline/natgas STORAGE series, so those are context-only
# everywhere — they were 3 of the 12 generated monthly columns, generated for no consumer.
LABOR_MONTHLY = PanelSpec(
    name="labor_monthly",
    note="payrolls / u3 / claims / bridge: 3 x 12 = 36 dims",
    columns=_scope(_MONTHLY_COLS, ("payems", "unrate", "claims")),
    **_MONTHLY,
)
INFLATION_MONTHLY = PanelSpec(
    name="inflation_monthly",
    note="cpi / pce: 4 x 12 = 48 dims (gas_retail is a cpi input, not decoration)",
    columns=_scope(_MONTHLY_COLS, ("cpi", "cpi_core", "pce_core", "gas_retail")),
    **_MONTHLY,
)
ENERGY_WEEKLY = PanelSpec(
    name="energy_weekly",
    note="energy: 4 x 13 = 52 dims. Also the lambda-calibration panel — the weekly Kalshi "
         "series (KXWTIW, KXNATGASW, KXAAAGASW, KXJOBLESSCLAIMS) are the only ones with a "
         "real sample to check the synthetic one against",
    columns=_scope(_WEEKLY_COLS, ("wti", "natgas", "rbob", "gas_retail")),
    **_WEEKLY,
)
CLAIMS_WEEKLY = PanelSpec(
    name="claims_weekly",
    note="claims / u3 at the frequency ICSA actually prints: 1 x 13 = 13 dims",
    columns=_scope(_WEEKLY_COLS, ("claims",)),
    **_WEEKLY,
)

# The quarterly column set, for KXGDP (#183). Three columns, and the two that are context
# were chosen against the calendar rather than for economic taste: A191RL1Q225SBEA reaches
# 1947Q2 and the binding constraint on the panel's start is whichever context column starts
# LATEST, because `build` drops rows until every column has one. UNRATE (1948-01) and
# CPIAUCSL (1947-01) cost essentially nothing; DGS2/DGS10 begin 1985 and ICSA 1967, and
# either would have thrown away half the history that makes this the healthiest panel in
# the project — 313 quarters for 5 output dims, against labor_monthly's 331 for 36.
#
# `transform="level"` and not `diff`. Quarterly real GDP growth is ALREADY a rate of change
# and already stationary (mean ~3.1%, no unit root); differencing it a second time would
# over-difference, manufacture a −0.5 MA(1) in the increments and make the generator learn
# to reverse a shock it should merely forget. `_from_increment` returns a "level" path
# unchanged, so the round trip is the identity and the settlement transform reads the rate
# itself, which is exactly what the market settles on.
#
# `prints="first"` because KXGDP settles on the BEA ADVANCE estimate. The second and third
# estimates revise it — often by several tenths — and a panel trained on the latest vintage
# would be training on a number nobody can trade, the same distinction `_WEEKLY_COLS`
# records for ICSA.
_QUARTERLY_COLS = (
    Column("gdp", "fred", "A191RL1Q225SBEA", "first", "last", "level", "%saar"),
    Column("unrate", "fred", "UNRATE", "first", "last", "diff", "pct"),
    Column("cpi", "fred", "CPIAUCSL", "latest", "last", "pct100", "%qoq"),
)

# H=5: the KXGDP ladder trades the current quarter and four ahead, which is what
# `gdp._ar1_offquarter` is built for (k=0..4). level_lag=20 is the monthly panel's five
# years expressed in quarters; VOL_LAG is a module constant in periods, so 12 quarters of
# vol window comes along with it and `back` = max(12, 20) = 20.
#
# The COVID span starts a quarter earlier than the monthly panels'. Their "2020-02-01" is
# right for months — January 2020 was still normal — but the quarterly bucket labelled
# 2020-01-01 CONTAINS March, and real GDP fell 5.5% annualised in it. Starting the span at
# the month those panels use would have left the largest peacetime contraction on record
# inside the training set of a panel with 313 rows.
_QUARTERLY = dict(freq="QS", horizon=5, start="1948-01-01",
                  drop_spans=(("2020-01-01", "2021-01-01"),),
                  level_lag=20)

GDP_QUARTERLY = PanelSpec(
    name="gdp_quarterly",
    note="KXGDP: the advance real-GDP print alone, 1 x 5 = 5 dims. The nowcast the model "
         "anchors on is NOT generated jointly — 43 quarters carry both, against 313 that "
         "carry the truth — it is derived as truth + resampled error, which §4f licensed "
         "by testing the dependence rather than assuming it away",
    columns=_scope(_QUARTERLY_COLS, ("gdp",)),
    **_QUARTERLY,
)

PANELS: dict[str, PanelSpec] = {p.name: p for p in (
    CORE_MONTHLY, ENERGY_WEEKLY_WIDE,
    LABOR_MONTHLY, INFLATION_MONTHLY, ENERGY_WEEKLY, CLAIMS_WEEKLY,
    GDP_QUARTERLY,
)}

# How many trailing increments the condition vector summarises. `DRIFT_LAG` is "where has
# this been going lately" and `VOL_LAG` is "how noisy has it been" — the two pieces of
# state a forecaster would actually condition on, and the two the models themselves use
# (payrolls' 3-month base, its 24-month residual MAD).
DRIFT_LAG = 3
VOL_LAG = 12


@dataclass
class PanelData:
    """A built panel plus everything needed to invert it.

    `levels` is in natural units (dollars, thousands of jobs, percent) and is what
    `worlds.py` writes into a synthetic db. `inc` is the stationary matrix the generator
    sees. `Z`/`C` are the standardized training arrays. `lattice` is the publication grid
    measured off `levels` — see `measure_lattice`; it also rides in `scaler` so a saved
    generator can quantise without the panel it was fitted on.
    """
    spec: PanelSpec
    levels: pd.DataFrame
    inc: pd.DataFrame
    anchors: list[pd.Timestamp]
    Z: np.ndarray            # (n, H*d) standardized forward paths
    C: np.ndarray            # (n, c_dim) standardized conditions
    scaler: dict
    end: datetime
    lattice: dict = field(default_factory=dict)

    @property
    def n_eff_hint(self) -> float:
        """Independent-draw count, not row count. Anchors step one period and each row
        spans H periods, so consecutive rows share H-1 periods of path."""
        return len(self.anchors) / float(self.spec.horizon)

    def path_of(self, z_row: np.ndarray) -> pd.DataFrame:
        """One standardized row -> an (H, d) increment frame in natural increment units."""
        raw = z_row * self.scaler["sd"] + self.scaler["mu"]
        return pd.DataFrame(raw.reshape(self.spec.horizon, self.spec.d),
                            columns=self.spec.names)


# ── reading ──────────────────────────────────────────────────────────────────
def _read(fs: FeatureStore, col: Column, end: datetime) -> pd.Series:
    """One column's source series at its native frequency, PIT <= `end`."""
    if col.source == "fut":
        s, _ = fs.fut_closes(col.sid, end, n=100_000)
    elif col.prints == "first":
        s, _ = fs.fred_first_prints(col.sid, end)
    else:
        s, _ = fs.fred_series(col.sid, end)
    s = s.dropna()
    if s.empty:
        raise ValueError(f"{col.name}: no rows for {col.sid} at {end.isoformat()}")
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _resample(s: pd.Series, freq: str, agg: str) -> pd.Series:
    """Collapse to the panel frequency.

    A source already at (or slower than) the panel frequency is reindexed and
    forward-filled rather than aggregated — quarterly GDP inside a monthly panel is the
    same number for three months, and re-aggregating it would invent variation.
    """
    r = s.resample(freq)
    out = r.mean() if agg == "mean" else r.last()
    return out.ffill()


def _payems_printed(conn, end: datetime, freq: str) -> pd.Series:
    """The NFP change AS PRINTED — level(t) minus level(t-1) read from the same first
    vintage of t. Imported from the model rather than re-derived: a plain difference of
    the first-print level chain mixes two vintages and therefore silently folds the
    revision to t-1 into the change, which is not the number the market settles on.
    Units are jobs (`payrolls.printed_changes` multiplies PAYEMS' thousands by 1000)."""
    from prediction_market_macro.model.payrolls import printed_changes
    s = printed_changes(conn, end)
    s.index = pd.to_datetime(s.index)
    return s.sort_index().resample(freq).last()


_INC_FNS = {"payems_printed": _payems_printed}


def _to_increment(level: pd.Series, transform: str, scale: float = 1.0) -> pd.Series:
    if transform == "level":
        return level
    if transform == "diff":
        return level.diff() * scale
    if transform == "dlog":
        pos = level.where(level > 0)
        return np.log(pos).diff()
    if transform == "pct100":
        pos = level.where(level > 0)
        return 100.0 * (np.log(pos).diff())
    raise ValueError(transform)


def _from_increment(inc: np.ndarray, anchor: float, transform: str,
                    scale: float = 1.0) -> np.ndarray:
    """Invert `_to_increment` forward from a known anchor level. Returns H levels."""
    if transform == "level":
        return np.asarray(inc, dtype=float)
    if transform == "diff":
        return anchor + np.cumsum(np.asarray(inc, dtype=float) / scale)
    if transform == "dlog":
        return anchor * np.exp(np.cumsum(inc))
    if transform == "pct100":
        return anchor * np.exp(np.cumsum(np.asarray(inc, dtype=float) / 100.0))
    raise ValueError(transform)


# ── the print lattice ────────────────────────────────────────────────────────
# Every series in this panel is PUBLISHED on a grid. PAYEMS is printed to the nearest
# thousand persons, UNRATE to one decimal, the CPI indices to three, a futures settlement
# to a tick. A diffusion emits none of them, and the gap is neither cosmetic nor small:
#
# * **Validity.** Measured 2026-08-26 on the cached C2ST pools, the real `labor_monthly`
#   increments are 100.0% on a 1000-job grid (payems) and a 0.1pp grid (unrate); both DFM
#   arms are 0.0%; `boot`/`knn`, which resample real rows, are 100.0%. A classifier
#   splitting on the fractional part therefore separates the classes with one threshold,
#   which is exactly the observed C2ST AUC of 1.000 — a number no amount of model quality
#   could have moved, because it was measuring a missing discretisation and nothing else.
#   `auc1_probe` had already ruled out a scaling bug (max univariate |AUC-0.5| = 0.026),
#   and the best single dispersion scalar only reaches 0.72, so this is the remainder.
# * **Utility.** Kalshi settles on the PRINTED value. A ladder strike sits AT a grid point
#   (KXU3 at 4.2/4.3, KXPAYROLLS on 25k boundaries), so an un-quantised synthetic world
#   puts probability mass on outcomes the settlement rule cannot produce and hands the
#   parameter argmin a bucket structure the real process does not have.
#
# The grid is MEASURED from the real levels, never asserted from what the release format is
# believed to be, because the panel's own resampling MOVES it. `agg="mean"` over a window of
# four-or-five sub-periods does not destroy the grid, which is what an earlier version of
# this comment claimed; it DIVIDES it by twenty. Averaging values that are multiples of `g`
# over 4 points lands on `g/4` and over 5 points on `g/5`, so a series whose months are a mix
# of the two sits exactly on `gcd(g/4, g/5) = g/20` and on nothing coarser. That is a
# derivation, not a fit, and it is confirmed on every mean-aggregated column in the repo
# (#203, §4e-I): ICSA's 1000 → 50, DGS2/DGS10's 0.01 → 0.0005, GASREGW's 0.001 → 0.00005,
# the EIA stock series' 1.0 → 0.05. `_LATTICE_STEPS` below contains `g/20` for exactly one of
# those four (0.05) and misses the other three, which is why `_exact_gcd_step` exists and why
# the ladder is no longer the only thing consulted.
_LATTICE_STEPS = (1000.0, 100.0, 10.0, 1.0, 0.5, 0.25, 0.1, 0.05, 0.01, 0.005, 0.001, 1e-4)
_LATTICE_TOL = 1e-6      # |x/g - round(x/g)| for a value stored at full float64 precision
_LATTICE_HIT = 0.995     # fraction of real levels that must sit exactly on the grid
_LATTICE_MIN_PTS = 20    # distinct grid points the real series must occupy
_F32_EPS = float(np.finfo(np.float32).eps)


def on_lattice(x: np.ndarray, step: float, tol: float = _LATTICE_TOL,
               dtype: str = "float64") -> float:
    """Fraction of `x` sitting on the `step` grid, to the precision `dtype` can express.

    `dtype="float32"` widens the tolerance to the storage error of a float32 round trip,
    which is what the futures columns need: `fut_closes` hands back 71.41000366 for a
    settlement of 71.41, so the tick grid is unmistakably present and not one value is an
    exact multiple of it.
    """
    q = np.asarray(x, dtype=float) / step
    q = q[np.isfinite(q)]
    if not len(q):
        return 0.0
    t = tol if dtype == "float64" else np.maximum(tol, 4.0 * _F32_EPS * np.abs(q))
    return float((np.abs(q - np.round(q)) < t).mean())


def _exact_gcd_step(x: np.ndarray) -> float | None:
    """The coarsest grid `x` sits on EXACTLY, found with no candidate ladder at all.

    `_LATTICE_STEPS` is hand-written, and it is incomplete in a way that is not random. Three
    of this repo's columns are `agg="mean"` over a 4-or-5 sub-period window, which by the
    derivation at the top of this section puts them on `g/20`. The ladder happens to contain
    `g/20` for `g = 1.0` and to omit it for `g = 1000`, `g = 0.01` and `g = 0.001` — so it
    returned 10 where the truth is 50, 0.0001 where the truth is 0.0005, and *nothing* where
    the truth is 0.00005 (0.00005 is finer than the ladder's finest rung, so no mantissa could
    have saved it). Four columns across three panel specs were being quantised onto a grid
    five times finer than the real series occupies, which means four of every five grid
    classes the generator emitted were values the publication process cannot produce.

    So this does not guess. Every finite value is scaled to an integer and the integer GCD is
    taken; the result is exact on 100% of the rows by construction, not on `_LATTICE_HIT` of
    them, and there is nothing to tune.

    **Why it is a second opinion and not a replacement.** Being exact on 100% is also its
    weakness: one bad row drags the GCD to the resolution floor, where `_best_step`'s 0.995
    tolerance would have shrugged it off. It is therefore only ever allowed to make the answer
    COARSER (`_best_step` applies that rule, not this function), so a row that ruins the GCD
    costs nothing — the ladder's answer stands.

    **The floor.** `8 * eps32 * max|x|` is borrowed from `_best_step`'s float32 pass, and the
    borrowing is not a free ride: there it means "finer than the storage can express", here
    the data is float64 and it means something weaker — below about 1e-7 of the series' own
    scale, "every value is a multiple of `g`" stops being a statement about a publication
    process and starts being one about float64 bookkeeping. The tightest real case clears it
    by only about 10x (`gas_retail` at 5e-05 against a floor of 4.8e-06), so it is a real
    fence and not a formality.
    """
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    if not len(v):
        return None
    top = float(np.abs(v).max())
    if top <= 0.0:
        return None
    # Ten significant decimal digits below the largest magnitude: comfortably inside float64's
    # ~15-16, and it keeps the scaled integers under 1e10 so the int64 GCD cannot overflow.
    base = 10.0 ** (math.ceil(math.log10(top)) - 10)
    q = v / base
    r = np.round(q)
    if not np.all(np.abs(q - r) < 1e-3):
        return None                      # not representable at this resolution — no verdict
    g = 0
    for n in np.unique(np.abs(r.astype(np.int64))):
        g = math.gcd(g, int(n))
        if g == 1:
            return None                  # resolution-limited, i.e. no grid worth the name
    if g <= 1:
        return None
    step = g * base
    return step if step >= 8.0 * _F32_EPS * top else None


def _best_step(x: np.ndarray) -> dict | None:
    """The COARSEST grid the series sits on as `{"step", "dtype"}`, or None for continuous.

    Two passes, and the order is what keeps a loose tolerance from inventing a grid.

    Pass 1 demands EXACTNESS. Everything read from FRED arrives at full float64 precision,
    so PAYEMS' thousand, UNRATE's 0.1 and the CPI indices' 0.001 are found here, and
    coarsest-first means a spuriously-passing fine step is never reached — at a level of
    159123 a step of 0.001 passes on rounding noise alone, but 1.0 has already been taken.

    Pass 2 runs only if pass 1 found nothing, and allows a float32 round trip. It is
    fenced by `g >= 8 * eps32 * max|x|`: a grid finer than the number's own storage
    resolution is undetectable in principle, and without that fence the widened tolerance
    would "find" a 1e-4 grid in a series of 230000-sized levels, where it spans the entire
    interval and the test is vacuous. Requiring pass 1 to fail first also means no float64
    column can ever be judged by the loose rule.

    `_LATTICE_MIN_PTS` stops a near-constant column from being declared to live on a grid
    it merely has too few distinct values to contradict.

    Pass 3 is `_exact_gcd_step`, and it is a **coarsening-only** override of pass 1. The
    ladder's answer is kept unless the GCD is strictly coarser and still occupies enough
    distinct points, so the GCD can correct an incompleteness in `_LATTICE_STEPS` (it does, on
    four columns) and can never invent a finer grid, never overrule a float32 verdict, and
    never turn a passing column into a failing one. If a single rogue row collapses the GCD,
    the collapsed value is not coarser and is therefore discarded — the failure mode is a
    no-op, which is the only reason an exact statistic is safe to consult at all.
    """
    v = np.asarray(x, dtype=float)
    v = v[np.isfinite(v)]
    if not len(v):
        return None
    found = None
    for dtype in ("float64", "float32"):
        floor = 0.0 if dtype == "float64" else 8.0 * _F32_EPS * float(np.abs(v).max())
        for g in _LATTICE_STEPS:
            if g < floor:
                continue
            if on_lattice(v, g, dtype=dtype) < _LATTICE_HIT:
                continue
            if len(np.unique(np.round(v / g))) < _LATTICE_MIN_PTS:
                continue
            found = {"step": float(g), "dtype": dtype}
            break
        if found is not None:
            break
    # A float32 column has already been judged by the loose rule, and an exact GCD of values
    # that are only approximately on their own grid is meaningless, so pass 3 is float64-only.
    if found is not None and found["dtype"] == "float32":
        return found
    exact = _exact_gcd_step(v)
    if exact is None:
        return found
    if found is not None and exact <= found["step"] * (1.0 + 1e-9):
        return found
    if len(np.unique(np.round(v / exact))) < _LATTICE_MIN_PTS:
        return found
    return {"step": float(exact), "dtype": "float64"}


def measure_lattice(levels: pd.DataFrame, spec: PanelSpec) -> dict[str, dict]:
    """Per GENERATED column, the grid DISCOVERED from the real level series.

    Columns with no grid are absent, so a caller reads "no entry" as "continuous" and never
    carries a None through the arithmetic.

    Discovered, not declared, and the difference is visible in the output: `gas_retail` is
    the SAME FRED series in two panels and comes back with a 0.001 grid in `energy_weekly`
    (agg="last", the published price survives) and 5e-05 in `inflation_monthly` (agg="mean",
    so 0.001/20 by the rule below). A hardcoded table would have forced GASREGW's three
    decimals onto a column that provably does not carry them.

    A consequence worth stating: this finds any exact grid the real data occupies, whatever
    produced it, and not only publication grids. Monthly `claims` is an average of four or
    five weekly ICSA prints and comes back on a grid of 50 — an artefact of the averaging
    (`gcd(1000/4, 1000/5)`; see `_LATTICE_STEPS` above and §4e-I), not a Bureau decision.
    That is still the right target, because the C2ST exploits an arithmetic regularity
    exactly as happily as an institutional one, and at 0.0003 of the column's increment sd
    it costs the sample nothing.

    WHAT THIS RETURNS IS THE POOLED GRID, AND THE POOLED GRID IS NOT THE ACHIEVABLE SET.
    Measured (§4e-K, #203): of `labor_monthly.claims`'s 715 months, the 467 with four ICSA
    prints sit on 250 and the 248 with five sit on 200 — each at a hit rate of 1.0000, each
    confirmed by an independent exact GCD. 50 is the gcd of those two lattices and so
    describes the POOLED column correctly, but the UNION of {multiples of 250} and
    {multiples of 200} covers only 40.3% of the 50-grid. A caller that rounds onto the scalar
    returned here therefore emits an unreachable number about 60% of the time, while real
    `claims` lands in that hole 0 times in 425 — a one-bit separator at AUC 0.800, against a
    pooled C2ST that cannot see the same defect at all (0.7854 vs 0.7853, §4e-I). Fixing it
    means a PERIOD-CONDITIONAL grid rather than a scalar, which changes generated levels, so
    it is registered as PR-17 and deliberately not slipped in here mid-A/B.
    """
    out: dict[str, dict] = {}
    for col in spec.gen_columns:
        if col.name not in levels.columns:
            continue
        g = _best_step(levels[col.name].to_numpy(dtype=float))
        if g is not None:
            out[col.name] = g
    return out


def _grid(entry) -> tuple[float, str] | None:
    """`(step, dtype)` from a lattice entry, accepting the bare float an older artefact
    may carry."""
    if not entry:
        return None
    if isinstance(entry, dict):
        return float(entry["step"]), str(entry.get("dtype", "float64"))
    return float(entry), "float64"


def quantise_levels(paths: np.ndarray, spec: PanelSpec,
                    lattice: dict | None) -> np.ndarray:
    """Round generated LEVELS onto each column's measured grid. Copy, never in place.

    Quantising the LEVEL rather than the increment is the only correct placement, and the
    two are not interchangeable: the grid lives on the printed number. For a `diff` column
    they happen to coincide (payems' 1.0-thousand level grid times scale 1000 is the
    1000-job increment grid), but for `dlog`/`pct100` the log change of grid-spaced levels
    is not itself grid-spaced, so rounding the increment would emit levels that are off-grid
    while claiming to be on it. Rounding the level and re-differencing (`to_increments`)
    also bounds the error at half a step forever, where difference-then-round lets it
    accumulate along the horizon.

    A float32 column is rounded to the grid AND THEN cast through float32, because matching
    the real class means matching its representation too. Emitting an exact 71.41 against a
    real class that stores 71.41000366 would not close the separability hole — it would
    invert it, and hand the classifier the same free split with the labels swapped.
    """
    out = np.array(paths, dtype=float, copy=True)
    for j, col in enumerate(spec.gen_columns):
        g = _grid((lattice or {}).get(col.name))
        if g is None:
            continue
        step, dtype = g
        q = np.round(out[..., j] / step) * step
        out[..., j] = q.astype(np.float32).astype(float) if dtype == "float32" else q
    return out


def integrate_paths(inc: np.ndarray, anchor_levels: pd.Series, spec: PanelSpec,
                    lattice: dict | None = None) -> np.ndarray:
    """(..., H, d) increments -> (..., H, d) levels, optionally on the print grid.

    The array-shaped twin of `integrate`, and the one `Generator.level_paths` delegates to
    so that the integrate-then-quantise sequence exists in exactly one place. A validity
    harness that re-implemented these two lines to save a second sampling pass would be
    scoring its own copy of production rather than production.
    """
    inc = np.asarray(inc, dtype=float)
    out = np.empty_like(inc)
    for j, col in enumerate(spec.gen_columns):
        a = float(anchor_levels[col.name])
        flat = inc[..., j].reshape(-1, inc.shape[-2])
        res = np.empty_like(flat)
        for i in range(len(flat)):
            res[i] = _from_increment(flat[i], a, col.transform, col.scale)
        out[..., j] = res.reshape(inc[..., j].shape)
    return quantise_levels(out, spec, lattice) if lattice else out


def to_increments(level_paths: np.ndarray, anchor_levels: pd.Series,
                  spec: PanelSpec) -> np.ndarray:
    """(..., H, d) levels + the anchor level -> the increments those levels imply.

    The exact inverse of `_from_increment`, vectorised over any leading axes. It exists so
    the validity harness can score the object production actually writes: `validate`
    compares INCREMENTS, `worlds.py` writes LEVELS, and without this round trip the
    quantisation would be invisible to every test that is supposed to police it.
    """
    lv = np.asarray(level_paths, dtype=float)
    out = np.empty_like(lv)
    for j, col in enumerate(spec.gen_columns):
        cur = lv[..., j]
        if col.transform == "level":
            out[..., j] = cur
            continue
        a = float(anchor_levels[col.name])
        prev = np.concatenate(
            [np.full(cur.shape[:-1] + (1,), a, dtype=float), cur[..., :-1]], axis=-1)
        if col.transform == "diff":
            out[..., j] = (cur - prev) * col.scale
        else:
            dl = np.log(np.maximum(cur, 1e-12)) - np.log(np.maximum(prev, 1e-12))
            out[..., j] = dl * (100.0 if col.transform == "pct100" else 1.0)
    return out


# ── condition vector ─────────────────────────────────────────────────────────
def _level_feature(x, transform: str):
    """The level as the condition should see it: log for multiplicative columns, so that
    "oil at 63" and "oil at 126" are one unit apart the way the increments are.
    Accepts a scalar or an array."""
    if transform in ("dlog", "pct100"):
        return np.log(np.maximum(np.asarray(x, dtype=float), 1e-9))
    return np.asarray(x, dtype=float)


def condition_row(levels: pd.DataFrame, inc: pd.DataFrame, spec: PanelSpec,
                  t: pd.Timestamp) -> np.ndarray:
    """The macro state at anchor `t`: level-vs-trend, recent drift, recent vol, calendar.

    Everything here is measured at or before `t`; nothing from the forward path leaks in.
    That is what `tests/test_synth_panel.py::test_condition_uses_no_future` pins.

    **Why the level enters as a deviation, not as itself.** The first build fed the raw
    (log) level in. Measured on the monthly panel, the leading principal component of the
    resulting condition matrix correlated **0.955 with calendar time** — CPI, core CPI,
    core PCE and PAYEMS are indices that only go up, so "the level" was very nearly "the
    year". A generator conditioned on the year cannot do anything at a held-out date except
    extrapolate off the end of its training range, and the held-out sweep showed exactly
    that: calibration degraded monotonically as more condition dimensions were admitted
    (cover80 0.74 unconditional -> 0.50 at the full 38 dims), i.e. conditioning was strictly
    harmful. Measuring each level against its own trailing `level_lag` mean keeps the part
    that is state ("oil is expensive relative to normal", "we are in a high-inflation
    regime") and discards the part that is a clock.

    The bet's own ABSOLUTE number is not lost by this: it enters through
    `Generator.level_paths`, which integrates the generated increments forward from today's
    real levels. The condition says what kind of environment this is; the anchor says where
    it starts.
    """
    i = levels.index.get_loc(t)
    feats: list[float] = []
    for col in spec.columns:
        win = levels[col.name].iloc[max(0, i - spec.level_lag + 1):i + 1].to_numpy()
        f = _level_feature(win, col.transform)
        feats.append(float(f[-1] - f.mean()))
    for col in spec.columns:
        past = inc[col.name].iloc[max(0, i - DRIFT_LAG + 1):i + 1]
        feats.append(float(past.mean()) if len(past) else 0.0)
    for col in spec.columns:
        past = inc[col.name].iloc[max(0, i - VOL_LAG + 1):i + 1]
        feats.append(float(past.std(ddof=0)) if len(past) > 1 else 0.0)
    # calendar: seasonality is real in claims and in every storage series, and a window's
    # whole seasonal position is determined by where it starts.
    ang = 2.0 * math.pi * (t.month - 1 + (t.day - 1) / 31.0) / 12.0
    feats += [math.sin(ang), math.cos(ang)]
    return np.asarray(feats, dtype=float)


def condition_dim(spec: PanelSpec) -> int:
    """Over ALL columns, generated or context-only — the condition is the environment."""
    return 3 * spec.d_all + 2


# ── build ────────────────────────────────────────────────────────────────────
def build(conn, name: str, end: datetime) -> PanelData:
    """Assemble the named panel from `conn`, using only data known at `end`.

    `end` is the generator's training cut. `param_argmin` runs its search over the last
    75 days, so callers building a panel to select on pass `now - 75d`: otherwise a set
    chosen on synthetic samples has, through the generator's fit, already seen the
    outcomes of the events it is about to be scored on. That is the diffuse version of
    the leak the grid75 protocol exists to prevent, and it costs nothing to close.
    """
    spec = PANELS[name]
    fs = FeatureStore(conn)
    cols: dict[str, pd.Series] = {}
    for col in spec.columns:
        cols[col.name] = _resample(_read(fs, col, end), spec.freq, col.agg)
    levels = pd.DataFrame(cols).loc[spec.start:]
    levels = levels.dropna()
    if levels.empty:
        raise ValueError(f"{name}: panel is empty after aligning columns from {spec.start}")
    incs = {}
    for c in spec.columns:
        if c.inc_fn:
            incs[c.name] = _INC_FNS[c.inc_fn](conn, end, spec.freq).reindex(levels.index)
        else:
            incs[c.name] = _to_increment(levels[c.name], c.transform, c.scale)
    inc = pd.DataFrame(incs).dropna()
    levels = levels.loc[inc.index]

    H = spec.horizon
    # An anchor needs `back` periods behind it (the longest window the condition uses) and
    # H ahead of it (the path). Anchors that cannot supply both are dropped rather than
    # padded — a padded condition is a fabricated state, and a SHORTER trailing window on
    # the early anchors would reintroduce the clock the deviation encoding exists to remove.
    idx = list(inc.index)
    back = max(VOL_LAG, spec.level_lag)
    kept = [k for k in range(len(idx)) if k >= back - 1 and k + H < len(idx)]
    for lo, hi in spec.drop_spans:
        lo_t, hi_t = pd.Timestamp(lo), pd.Timestamp(hi)
        kept = [k for k in kept if not (idx[k + 1] <= hi_t and idx[k + H] >= lo_t)]
    anchors = [idx[k] for k in kept]
    if not anchors:
        raise ValueError(f"{name}: no anchor supports {back} back + {H} forward")

    zs, cs = [], []
    for k, t in zip(kept, anchors):
        block = inc.iloc[k + 1:k + 1 + H][list(spec.names)].to_numpy(dtype=float)
        zs.append(block.reshape(-1))
        cs.append(condition_row(levels, inc, spec, t))
    Zr = np.asarray(zs, dtype=float)
    Cr = np.asarray(cs, dtype=float)
    mu, sd = Zr.mean(0), Zr.std(0) + 1e-12
    cmu, csd = Cr.mean(0), Cr.std(0) + 1e-12
    lattice = measure_lattice(levels, spec)
    scaler = {"mu": mu, "sd": sd, "cmu": cmu, "csd": csd,
              "names": spec.names, "horizon": H,
              "transforms": [c.transform for c in spec.gen_columns],
              "lattice": lattice}
    return PanelData(spec=spec, levels=levels, inc=inc, anchors=anchors,
                     Z=(Zr - mu) / sd, C=(Cr - cmu) / csd, scaler=scaler, end=end,
                     lattice=lattice)


def integrate(inc_path: pd.DataFrame, anchor_levels: pd.Series, spec: PanelSpec,
              lattice: dict | None = None) -> pd.DataFrame:
    """(H, d) increments + the level at the anchor -> (H, d) levels in natural units.

    `lattice` rounds the result onto the publication grid. It defaults to None — off — so
    that this stays the exact analytic inverse `test_integrate_inverts_the_transform`
    pins; the production path supplies it through `Generator.level_paths`.
    """
    out = {}
    for col in spec.gen_columns:
        out[col.name] = _from_increment(inc_path[col.name].to_numpy(dtype=float),
                                        float(anchor_levels[col.name]), col.transform,
                                        col.scale)
    df = pd.DataFrame(out, index=inc_path.index)
    if lattice:
        arr = quantise_levels(df[spec.names].to_numpy(dtype=float), spec, lattice)
        df = pd.DataFrame(arr, index=inc_path.index, columns=spec.names)
    return df
