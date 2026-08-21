"""synth/book.py — the market's side of a synthetic event, transplanted, never invented.

**The measurement this module is built around** (`docs/PLAN_DFM_SYNTH.md` §5b, 75 events /
496 replay-days): the real market's devigged mean sits where the outcome lands with
`corr(z_m, z_y) = +0.70`, and its error against the outcome is 0.78 of our own predictive
sd against our model's 0.90. **The market is the better forecaster on this sample.** A
synthetic book whose position were drawn independently of the outcome would therefore be an
uninformed counterparty, the strategy would print money against it, and every parameter
ranked on those worlds would be ranked on a fiction. Reproducing that dependence is the
whole job here; everything below is subordinate to it.

**So the unit of transplant is a whole event, not a day.** A donor carries its complete
replay-day trajectory — where the market sat, how wide it was, how much it charged, and
the *shape* of its devigged pmf — plus the ladder it was quoted on. All of it moves across
together, selected by nearest `z_y`. Drawing days independently would destroy the two
things worth preserving: the market's convergence toward the close, and its correlation
with the truth.

**What is standardized and why.** A donor from KXNATGASW cannot price a synthetic
KXPAYROLLS event in dollars per MMBtu. Everything a donor carries is therefore expressed
in units of *its own* book — offsets `(x − m_mean) / m_sd` — and re-expressed on the target
by the target's own `(m_mean*, m_sd*)`, which are built from the incumbent's prediction on
the synthetic world:

    m_mean*(d) = P_mean*(d) + z_m(d) · P_sd*(d)
    m_sd*(d)   = r(d) · P_sd*(d)

`z_m` and `r` come from the donor; `P_mean*`, `P_sd*` come from running the real model
function on the synthetic history. That is what makes the transplant carry the market's
*relationship* to our view rather than its absolute prices, which are meaningless off their
own series.

**What is NOT modelled, stated rather than hidden.** The donor's pmf shape is carried as
an empirical standardized histogram, so skew and bimodality survive; but a synthetic
market cannot react to a synthetic release, because the donor's reaction was to a different
one. Half-spreads are carried per replay day, not per leg-price bucket — the pooled
half-spread is 1¢ at the median and the price-bucket dependence is well inside the noise of
75 events. Both are simplifications of the market's side, both make the synthetic market
slightly EASIER to trade against than the real one, and `lambda` (S5) is measured against
real events precisely so that this optimism is paid for rather than believed.
"""
from __future__ import annotations

import importlib
import json
import math
from dataclasses import asdict, dataclass
from dataclasses import replace as _replace
from datetime import datetime
from pathlib import Path

import numpy as np

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.model.common import Categorical, grid_pmf
from prediction_market_macro.research import pnl_score as _ps
from prediction_market_macro.research.synth import worlds as _w
from prediction_market_macro.research.walkforward import _implied
from prediction_market_macro.util.periods import kalshi_period_to_key

# Kalshi quotes whole cents and `decide` refuses a leg outside these bounds anyway; a
# transplanted book that emitted 0.004 would be a book no venue could show.
TICK = 0.01
MIN_PRICE, MAX_PRICE = 0.01, 0.99


# ── moments of a devigged book ───────────────────────────────────────────────
def pinned_moments(pmf: dict[float, float], step: float) -> tuple:
    """(mean, sd, tail_mass) of a devigged pmf whose outermost buckets are open.

    `devig.ladder_implied` keys the tails ±inf because a ladder's extreme legs are
    open-ended, and a moment cannot be taken over an infinite support point. Dropping them
    would renormalise the market's view away from its own tails — free sharpness, in the
    direction that flatters us. Each is pinned one `round_rule` step past the outermost
    FINITE key, which is the convention `worlds._implied_outcome` already uses for a
    one-sided ladder, and the pinned mass is RETURNED so a caller can report how much of
    the book it had to assume.
    """
    fin = [k for k in pmf if math.isfinite(k)]
    if not fin:
        return None, None, None
    lo, hi = min(fin), max(fin)
    tail, pin = 0.0, {}
    for k, v in pmf.items():
        if math.isinf(k):
            tail += v
            k = (hi + step) if k > 0 else (lo - step)
        pin[k] = pin.get(k, 0.0) + v
    tot = sum(pin.values())
    if tot <= 0:
        return None, None, None
    m = sum(k * v for k, v in pin.items()) / tot
    sd = math.sqrt(max(sum((k - m) ** 2 * v for k, v in pin.items()) / tot, 0.0))
    return m, sd, tail / tot


# ── donors ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DonorDay:
    """One replay day of a donor event, in units of that day's own book."""
    dtc: float                       # days to close, the coordinate a target replays on
    z_m: float                       # (market mean - our mean) / our sd
    r: float                         # market sd / our sd
    half_spread: float               # mean (ask - bid) / 2 across quoted legs
    shape: list[tuple[float, float]]  # standardized ((x - m_mean)/m_sd, mass)
    tail_mass: float                 # of `shape`, how much was a pinned open bucket


@dataclass(frozen=True)
class Donor:
    """One real event's whole market side, ready to be re-expressed on another series."""
    series: str
    tok: str
    z_y: float                       # (outcome - our mean) / our sd, at the anchor day
    days: list[DonorDay]
    offsets: list[dict]              # ladder in standardized units (see `_ladder_offsets`)
    n_legs: int

    def day_at(self, dtc: float) -> DonorDay:
        """The donor day closest in time-to-close. Replay grids differ in length between
        donor and target (a 7-day window can hold 6 or 7 quoted days), and `dtc` is the
        only coordinate on which the two are commensurable — matching by index would pair
        a target's first day against a donor's day deep into its convergence."""
        return min(self.days, key=lambda d: abs(d.dtc - dtc))


def _ladder_offsets(meta: list[dict], m_mean: float, m_sd: float) -> list[dict]:
    """The quoted ladder in units of its own book: `(strike - m_mean) / m_sd`.

    Strike TYPE travels with the offset, because it is the settlement rule and not a
    presentation detail: KXWTIW quotes `between` buckets that partition the line, every
    other series quotes a `greater` survival ladder, and re-expressing one as the other
    would change what the legs pay, not merely where they sit.
    """
    out = []
    for m in meta:
        st = m.get("strike_type")
        lo, hi = m.get("strike"), m.get("cap_strike")
        out.append({"strike_type": st,
                    "floor_off": None if lo is None else (lo - m_mean) / m_sd,
                    "cap_off": None if hi is None else (hi - m_mean) / m_sd})
    return out


def measure(conn, series: str | None = None, log=None) -> list[Donor]:
    """Extract every real quotable event as a `Donor`.

    Anchored on the INCUMBENT prediction (`params=None`) throughout, for the reason
    `pnl_score.gate_history` builds gate state once from the incumbent: a candidate set
    that was never deployed did not move the real market either, so letting the descriptor
    move with the candidate would credit a parameter set for a book it did not cause.
    """
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    want = [series] if series else list(REGISTRY)
    gates = _ps.wf_gates()
    donors: list[Donor] = []
    for s in want:
        spec = REGISTRY[s]
        if spec.structure == "categorical":
            # No numeric mean exists, so no standardized descriptor does either. Skipped
            # loudly: KXFEDDECISION simply has no donor pool and cannot be synthesized.
            if log:
                log(f"{s}: categorical — no numeric book descriptor, no donors")
            continue
        disp = SERIES_DISPATCH[s]
        fn = getattr(importlib.import_module(disp[0]), disp[1])
        for ev in _ps.quotable_events(conn, s):
            key = kalshi_period_to_key(ev["tok"])
            if not key:
                continue
            try:
                plan = _w.read_event(conn, s, ev["tok"])
            except ValueError as exc:
                if log:
                    log(f"  {s}/{ev['tok']}: {exc}")
                continue
            y = plan.outcome
            if not isinstance(y, (int, float)):
                continue
            days, offsets, z_y, n_legs = [], None, None, 0
            for day in _ps.entry_days(ev["close_ts"], gates):
                meta, _res = _ps._legs_at(conn, s, ev["tok"], day)
                if not meta:
                    continue
                try:
                    pred = fn(conn, day, key, series=s, params=None)
                except Exception:                                    # noqa: BLE001
                    continue
                if isinstance(pred.dist, Categorical):
                    continue
                p_mean, p_sd, _ = pinned_moments(grid_pmf(pred.dist, spec.round_rule),
                                                 spec.round_rule)
                if not p_sd:
                    continue
                im = _implied(meta)
                if not im.get("pmf"):
                    continue
                book = {float(k): v for k, v in im["pmf"].items()}
                m_mean, m_sd, tail = pinned_moments(book, spec.round_rule)
                if m_mean is None or not m_sd:
                    continue
                sp = [m["yes_ask"] - m["yes_bid"] for m in meta
                      if m["yes_ask"] is not None and m["yes_bid"] is not None]
                if not sp:
                    continue
                shape = []
                for k, v in sorted(book.items()):
                    if math.isinf(k):
                        k = (max(x for x in book if math.isfinite(x)) + spec.round_rule
                             if k > 0 else
                             min(x for x in book if math.isfinite(x)) - spec.round_rule)
                    shape.append(((k - m_mean) / m_sd, v))
                days.append(DonorDay(
                    dtc=(ev["close_ts"] - day).total_seconds() / 86400.0,
                    z_m=(m_mean - p_mean) / p_sd, r=m_sd / p_sd,
                    half_spread=sum(sp) / len(sp) / 2.0, shape=shape, tail_mass=tail))
                if offsets is None:                 # the ladder as first quoted
                    offsets = _ladder_offsets(meta, m_mean, m_sd)
                    z_y = (y - p_mean) / p_sd
                    n_legs = len(meta)
            if days and offsets:
                donors.append(Donor(series=s, tok=ev["tok"], z_y=z_y, days=days,
                                    offsets=offsets, n_legs=n_legs))
        if log:
            log(f"{s}: {sum(d.series == s for d in donors)} donors")
    return donors


def save(donors: list[Donor], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(d) for d in donors]))
    return path


def load(path: Path | str) -> list[Donor]:
    raw = json.loads(Path(path).read_text())
    return [Donor(series=d["series"], tok=d["tok"], z_y=d["z_y"],
                  days=[DonorDay(dtc=x["dtc"], z_m=x["z_m"], r=x["r"],
                                 half_spread=x["half_spread"],
                                 shape=[tuple(p) for p in x["shape"]],
                                 tail_mass=x["tail_mass"]) for x in d["days"]],
                  offsets=d["offsets"], n_legs=d["n_legs"]) for d in raw]


# ── drawing ──────────────────────────────────────────────────────────────────
def zm_slope(donors: list[Donor]) -> float:
    """The real pooled regression slope d(z_m)/d(z_y), over every donor day-row.

    This is the §5b dependence stated as a coefficient rather than a correlation, and it is
    what `draw` uses to place a donor at the target's condition. Measured on the pool it is
    handed, never a constant: a pool rebuilt after more events have settled must re-measure,
    and a hardcoded +0.29 would go stale silently.
    """
    x = np.array([d.z_m for e in donors for d in e.days], dtype=float)
    y = np.array([e.z_y for e in donors for _ in e.days], dtype=float)
    if len(x) < 3 or y.std() == 0:
        raise ValueError("zm_slope: donor pool has no usable z_y variation")
    return float(np.polyfit(y, x, 1)[0])


def draw(donors: list[Donor], z_y: float, rng: np.random.Generator,
         k: int = 10, exclude_series: str | None = None,
         slope: float | None = None) -> Donor:
    """A donor whose outcome landed about as far from our view as this synthetic one did,
    RE-ALIGNED onto the target's own `z_y`.

    This draws the market's VIEW — `z_m`, `r`, the pmf shape, the spread — and nothing
    else. The ladder it is quoted on comes from `draw_ladder`, for reasons set out there.

    Nearest-`z_y` and then uniform among the k nearest, rather than the single nearest: with
    75 donors the single nearest would be reused across many synthetic events and its
    idiosyncrasies — one wide book, one bad spread day — would become a systematic feature
    of the synthetic sample rather than a draw from it.

    **Why the re-alignment, and why it cannot overshoot.** k=10 buys that diversity with a
    `z_y` mismatch — measured at 0.24 on average, and BIASED −0.12 because the synthetic
    outcomes sit above the donor pool's centre so the k nearest are asymmetrically below.
    Carried through untouched, that mismatch attenuates the very dependence this module
    exists to reproduce (+0.57 real, +0.51 after selection alone) and tilts the whole
    synthetic market low. So each donor day is shifted

        z_m*(d) = z_m(d) + slope · (z_y_target − z_y_donor)

    Writing the donor's own structure as `z_m = a + slope·z_y_donor + e`, the shift gives
    exactly `a + slope·z_y_target + e` — the real regression evaluated at the target's
    condition, carrying the donor's OWN residual `e` unchanged. It therefore restores the
    real slope and the real residual scatter and nothing more; it cannot manufacture a
    dependence tighter than the one measured, because the noise term is not touched.

    `exclude_series` exists for validation, not for production use: leaving a series' own
    donors out is how you check the pooling assumption actually holds, and using it while
    generating would throw away the most relevant donors for no reason.
    """
    pool = [d for d in donors if d.series != exclude_series] if exclude_series else donors
    if not pool:
        raise ValueError("draw: donor pool is empty")
    order = sorted(pool, key=lambda d: abs(d.z_y - z_y))
    near = order[:min(k, len(order))]
    d = near[int(rng.integers(len(near)))]
    b = zm_slope(donors) if slope is None else slope
    shift = b * (z_y - d.z_y)
    return _replace(d, z_y=z_y,
                    days=[_replace(x, z_m=x.z_m + shift) for x in d.days])


def draw_ladder(donors: list[Donor], series: str,
                rng: np.random.Generator) -> list[dict]:
    """Ladder geometry from the TARGET series' own real events — never from the view donor.

    **This split is a measured correction, not a preference.** Quoting the view donor's own
    ladder means a 40-leg KXNATGASW geometry can land on a KXJOBLESSCLAIMS event, and it
    does not fit: measured over 8 synthetic paths, 49% of quotes pinned at the 1c/99c band
    against 20% on the real KXJOBLESSCLAIMS book. A pinned ladder carries almost no
    information, `devig` inverts what is left into a distorted pmf, and the delivered
    dependence collapsed to +0.26 against the +0.57 the donors carry — the transplant was
    destroying the one relationship it exists to preserve.

    The two things are different in kind, which is why separating them is the honest move
    rather than a patch. How many legs Kalshi lists and how far apart it spaces them is a
    VENUE fact about a series, observable and near-constant within it. Where the market
    sits and how confident it is, is the market's VIEW, and that is what pooling across
    series buys — the §5b measurement found the `(z_m, r)` relationship holds on all 12
    fittable series, so a view is portable in a way a ladder is not.

    A series with only one or two donors gets a nearly deterministic ladder here, and that
    is fine for the same reason: its real geometry barely varies. A series with NO donors
    raises, because there is no observed geometry to quote and inventing one would be
    fabricating the market structure rather than transplanting it.
    """
    pool = [d for d in donors if d.series == series]
    if not pool:
        raise ValueError(f"draw_ladder: no donor of {series!r} — its ladder geometry has "
                         "never been observed and cannot be invented")
    return pool[int(rng.integers(len(pool)))].offsets


def coverage(donors: list[Donor], z_ys) -> dict:
    """How far outside the donor pool the synthetic outcomes fall — the §5b S4 gate.

    If the incumbent's `P_sd` on synthetic worlds differs systematically from its `P_sd` on
    real ones, `z_y*` lands beyond every donor and each draw is an extrapolation dressed as
    a transplant. That is measurable, so it is measured: `outside` is the fraction of
    synthetic events with no donor within `max_gap` in `z_y`, and a caller that sees it
    rise should widen the panel or narrow its claims, not carry on.
    """
    dz = np.array([d.z_y for d in donors], dtype=float)
    z = np.asarray(list(z_ys), dtype=float)
    gaps = np.array([np.min(np.abs(dz - v)) for v in z]) if len(z) else np.array([])
    return {"n_donors": len(dz), "n_synth": int(len(z)),
            "donor_range": [float(dz.min()), float(dz.max())] if len(dz) else None,
            "synth_range": [float(z.min()), float(z.max())] if len(z) else None,
            "median_gap": float(np.median(gaps)) if len(gaps) else None,
            "p95_gap": float(np.percentile(gaps, 95)) if len(gaps) else None,
            "outside": float(np.mean(gaps > 0.5)) if len(gaps) else None}


# ── building the target's book ───────────────────────────────────────────────
def _snap(x: float, step: float) -> float:
    return round(round(x / step) * step, 10)


def _mapped_pmf(day: DonorDay, m_mean: float, m_sd: float,
                step: float) -> dict[float, float]:
    """The donor's standardized market shape, re-expressed on the target's scale and grid.

    Snapped to the target's `round_rule` because the LEGS are snapped to it too, and a
    support point sitting a hair above a strike it should have been level with flips that
    leg's probability by its whole mass. Snapping both to one grid makes ties resolve the
    same way they did in the donor's own book.
    """
    out: dict[float, float] = {}
    for u, mass in day.shape:
        x = _snap(m_mean + u * m_sd, step)
        out[x] = out.get(x, 0.0) + mass
    tot = sum(out.values())
    return {k: v / tot for k, v in out.items()} if tot > 0 else {}


def _leg_prob(pmf: dict[float, float], leg: dict, strict_gt: bool) -> float:
    """P(this leg settles YES) under `pmf`, by asking `worlds.settle_leg` support point
    by support point.

    Pricing and settlement MUST agree or the synthetic market quotes a different contract
    than the synthetic settlement pays: a leg priced as `>=` and settled as `>` is a free
    edge at the money, which is exactly where the size goes. Rather than restate the four
    strike rules here and hope the two copies stay in step, this calls the settlement
    function itself, so divergence is not merely unlikely but unrepresentable. It also
    inherits `settle_leg`'s refusal of custom/NULL legs for free.
    """
    return sum(v for x, v in pmf.items() if _w.settle_leg(leg, x, strict_gt) == "yes")


def build_ladder(offsets: list[dict], series: str, period: str, m_mean: float,
                 m_sd: float) -> list[dict]:
    """A ladder geometry, re-expressed around the target's book and snapped to its grid.

    `offsets` comes from `draw_ladder` — the target series' OWN observed geometry. It is
    taken as a bare list rather than a `Donor` so that the call site cannot quietly pass
    the view donor and reintroduce the mismatch `draw_ladder` documents.

    Snapping can collapse two strikes onto one — a geometry observed at a book width the
    target's `round_rule` cannot resolve. Those are DEDUPLICATED rather than kept, because
    two legs with identical bounds are the same contract quoted twice, and
    `enumerate_structs` would build spreads between them with a guaranteed-zero payoff.
    """
    spec = REGISTRY[series]
    step = spec.round_rule
    legs, seen = [], set()
    for off in offsets:
        st = off["strike_type"]
        lo = None if off["floor_off"] is None else _snap(m_mean + off["floor_off"] * m_sd,
                                                         step)
        hi = None if off["cap_off"] is None else _snap(m_mean + off["cap_off"] * m_sd, step)
        sig = (st, lo, hi)
        if sig in seen:
            continue
        seen.add(sig)
        legs.append({"ticker": f"{series}-{period}-L{len(legs):02d}",
                     "strike_type": st, "floor_strike": lo, "cap_strike": hi,
                     "sub_title": None})
    return legs


def quote(donor: Donor, legs: list[dict], series: str,
          per_day: list[tuple[datetime, float, float]], close: datetime) -> dict:
    """(ticker -> [(end_ts, yes_bid, yes_ask)]) for a synthetic event.

    `per_day` is [(asof, P_mean*, P_sd*)] — the incumbent's prediction on the SYNTHETIC
    world at each replay day, which is what carries the generated macro path into the
    market's position. One candle per replay day, stamped at exactly the asof
    `pnl_score.entry_days` produces, because `_candle_quote` takes the last candle at or
    before the asof and an off-by-one-hour stamp silently drops the first day.

    `close` is passed rather than taken as the last replay day: `entry_days` stops at
    16:00 UTC on the last day BEFORE the close, so inferring it from `per_day` would put
    every target's final day at dtc=0 and pair it against donor days that were still
    hours or a full day out. Days-to-close is the coordinate the whole transplant is
    aligned on, and it has to be measured from the same origin on both sides.

    Prices are the donor's own half-spread around the transplanted fair, clipped to
    Kalshi's tradable band. A leg pushed to the clip is still written: an 0.99 leg is a
    real thing a real book shows, and dropping it would quietly shrink synthetic ladders at
    exactly the strikes where the outcome was most predictable.
    """
    spec = REGISTRY[series]
    book: dict[str, list[tuple[int, float, float]]] = {l["ticker"]: [] for l in legs}
    for asof, p_mean, p_sd in per_day:
        dtc = (close - asof).total_seconds() / 86400.0
        day = donor.day_at(dtc)
        m_mean = p_mean + day.z_m * p_sd
        m_sd = max(day.r * p_sd, spec.round_rule / 4.0)
        pmf = _mapped_pmf(day, m_mean, m_sd, spec.round_rule)
        if not pmf:
            continue
        end_ts = int(asof.timestamp())
        for leg in legs:
            p = _leg_prob(pmf, leg, spec.strict_gt)
            bid = min(max(round((p - day.half_spread) / TICK) * TICK, MIN_PRICE), MAX_PRICE)
            ask = min(max(round((p + day.half_spread) / TICK) * TICK, MIN_PRICE), MAX_PRICE)
            if ask < bid:                       # both clipped to the same edge of the band
                ask = bid
            book[leg["ticker"]].append((end_ts, round(bid, 2), round(ask, 2)))
    return book
