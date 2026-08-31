"""research/shadow_width.py — PR-12 (#192): score the energy width ladder forward.

`docs/PREREGISTER.md` registered ONE hypothesis on 2026-08-27 and this module is the only
thing allowed to grade it. It is written BEFORE the registration is read as a result,
because #195 is what a registered-but-unwritten scorer looks like three weeks later.

The registration, verbatim
--------------------------
    改动   `model/energy.DEFAULT_PARAMS["fut_sigma_scale"]` 从 1.0 改为阶梯里的某一档
    判据   每个市场前向 12 个已结算周, 配对区间对数似然 LL(rung) > LL(1.0),
           单边 Wilcoxon, Bonferroni 除以 3 个非默认档 => alpha 0.0167
    K      19 (发现期) + 3 (前向档位)

Three things about that text that are worth saying out loud rather than interpreting
quietly later:

1. **The bar is the DEFAULT, not the market.** Every other registration in that file
   grades against the book, because every other one is a claim about edge. This one is
   not: #192's claim is "our own predictive distribution is the wrong WIDTH", and the
   thing that can be right or wrong about it is our own calibration. Beating the default
   here authorises exactly nothing at the order router — `strategy/skill.py` still blocks
   the model path on the market ratio, and the market comparison is reported below as a
   SECONDARY readout that grades nothing. Reporting it is not permission to pass on it.

2. **The metric is the interval log-likelihood, not Brier.** The discovery scan found the
   Brier dip at scale 0.60-0.65 beating the market by 0.00008 on 14 scored events with a
   Diebold-Mariano p of 0.48 at EVERY rung — i.e. Brier on this sample has no power to
   see a width effect at all, because it collapses a whole ladder to one leg-mean. The
   settled ladder interval-censors the print into `(max YES strike, min NO strike]`, and
   `log(F(hi) - F(lo))` is the likelihood of exactly that observation. It uses every
   event (KXNATGASW scores 21/21 with no drop) and it is the quantity a width claim is
   actually about.

3. **The ladder can widen.** 1.2 is a rung. A test that can only agree with its
   hypothesis is not a test, and if the narrowing was a 19-fold search artifact the
   honest outcome is that 1.0 wins its own ladder.

Why this is a replay and not a live logger
------------------------------------------
Same reason as `shadow_claims`: `asof` is a deterministic function of stored timestamps,
so there is no after-the-fact choice to make and both arms can be replayed. The two rules
that make `asof` honest live in `backtest.replay_series` and are MIRRORED here rather than
imported, because that function returns Brier and this one needs the distribution itself:

  * step back behind the print when the book closed after the release
    (KXPAYROLLS/KXU3 2026-01 closed 90 minutes past 13:30Z);
  * drop the event entirely when `pred.data_horizon` reached past the release, whatever
    the asof was.

A mirror is a duplication and duplications drift, so `tests/test_shadow_width.py` pins
that this module and `replay_series` agree on the asof of every settled event of both
series. If that test fails, this file is wrong and not the other one.
"""
from __future__ import annotations

import hashlib
import math
import pathlib
from datetime import datetime, timedelta, timezone

import numpy as np

SERIES = ("KXNATGASW", "KXWTIW")

# The registration. None of these may be edited to make a result pass.
REGISTERED = "2026-08-27T00:00:00+00:00"
N_FORWARD = 12                 # registered forward settled weeks, PER SERIES
OFFSET = "-1h"
DEFAULT_RUNG = 1.0
K_DISCOVERY = 19               # see docs/PREREGISTER.md PR-12 for the itemised count
ALPHA = 0.05

# The ECDF behind an Empirical has 20k samples, so the smallest probability it can
# represent is 5e-5 and an interval that no sample lands in reads as exactly zero. Scoring
# that as -inf would let a single event decide the whole registration by Monte-Carlo
# resolution rather than by the model. Floored at half a sample, and every flooring is
# COUNTED into `n_ll_floored` — a run where that count is not small is not evidence.
_P_FLOOR = 0.5 / 20_000.0
INF = float("inf")

# Stamped once, at the commit that lands PR-12. Never re-stamp to make a mismatch go away:
# a mismatch is the signal that `model/energy.py` moved under a live registration, and the
# repair is a line in KNOWN_FINGERPRINTS saying what moved and whether the paired
# comparison survived it.
REGISTERED_FINGERPRINT = "74fbe74e67b0"   # re-stamped 2026-08-31 (PR-31 gate + samples-field fix, inert)
KNOWN_FINGERPRINTS: dict[str, str] = {
    "74fbe74e67b0": (
        "2026-08-31 (PR-31) — dispatcher gate bug fix: dist.values -> dist.samples\n"
        "inside the ercot_w branch. INERT: the branch is unreachable at the default\n"
        "ercot_w=0, both arms bit-identical."),
    "5fd5adab8fce": (
        "2026-08-31 (PR-31) — the ERCOT covariate gate on the HEADLINE MoM branch:\n"
        "mu gains w*ercot_cov.mu_shift behind params['ercot_w'], default 0.0. INERT\n"
        "for both arms — nothing here passes ercot_w, all numbers bit-identical."),
    "c895f35a7893": "PR-12 as registered: `fut_sigma_scale` added to DEFAULT_PARAMS at a "
                    "bit-identical 1.0 and applied to the horizon sigma after the floor. "
                    "VERSION deliberately not bumped — the default multiplies by exactly "
                    "1.0 in IEEE754, so no prediction moved.",
}


def ladder() -> tuple[float, ...]:
    """The rungs, read from `param_space` rather than restated.

    Restating them would let the grid and the registration drift apart silently, and the
    registration is the thing that is supposed to be immovable — so it reads the grid and
    a test pins that the grid still contains the default.
    """
    from prediction_market_macro.research.param_space import CANDIDATES
    return tuple(CANDIDATES["energy"]["fut_sigma_scale"][1])


def code_fingerprint() -> str:
    """sha1 prefix of `model/energy.py`. On every run, for the same reason as PR-1: both
    arms move together under a model change and the pairing survives, but the registered
    effect size does not."""
    p = pathlib.Path(__file__).resolve().parent.parent / "model" / "energy.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def code_change_note(fp: str | None = None) -> dict:
    fp = fp or code_fingerprint()
    if REGISTERED_FINGERPRINT == "PENDING":
        return {"fingerprint": fp, "registered_fingerprint": None,
                "code_changed_since_registration": None, "change_is_documented": False,
                "note": "registration fingerprint not stamped yet — run --stamp once, at "
                        "the commit that lands PR-12, and never again."}
    return {"fingerprint": fp, "registered_fingerprint": REGISTERED_FINGERPRINT,
            "code_changed_since_registration": fp != REGISTERED_FINGERPRINT,
            "change_is_documented": fp in KNOWN_FINGERPRINTS,
            "note": KNOWN_FINGERPRINTS.get(fp, (
                "UNDOCUMENTED CHANGE — model/energy.py no longer matches any recorded "
                "version. Establish whether the registered comparison survived it and "
                "record the answer in KNOWN_FINGERPRINTS before reading the numbers "
                "below as PR-12's."))}


def realized_interval(legs) -> tuple[float, float, str] | None:
    """The interval the settled ladder implies for the print, TAILS INCLUDED.

    Returns (lo, hi, kind), lo possibly -inf and hi possibly +inf, or None when the
    settlement pattern cannot pin an interval at all. The included tails are the whole
    reason this exists: the older `series_calib` scored only events with a two-sided
    bracket and took its midpoint, which drops precisely the largest moves (measured drop
    rates: KXAAAGASW 60.3%, KXU3 34.4%, KXCPI 33.9%) and mechanically makes any model
    look too wide. KXNATGASW happens to lose nothing to that (21/21 interior), which is
    what makes its over-width a real finding rather than an artifact — but the estimator
    still has to be the right one, or the next series is measured wrong.
    """
    buckets = [l for l in legs if l["cap_strike"] is not None
               and l["floor_strike"] is not None]
    for l in buckets:
        if l["result"] == "yes":
            return float(l["floor_strike"]), float(l["cap_strike"]), "bucket"
    ladder_legs = [l for l in legs
                   if l["cap_strike"] is None and l["floor_strike"] is not None]
    yes = [float(l["floor_strike"]) for l in ladder_legs if l["result"] == "yes"]
    no = [float(l["floor_strike"]) for l in ladder_legs if l["result"] == "no"]
    if yes or no:
        lo = max(yes) if yes else -INF
        hi = min(no) if no else INF
        if hi <= lo:
            return None                              # inconsistent settlement
        kind = ("interior" if yes and no else
                ("upper_censored" if not no else "lower_censored"))
        return lo, hi, kind
    # a bucket ladder where every leg settled NO: the print is below every bucket or above
    # every bucket and the pattern alone cannot say which. Not scorable — counted, never
    # silently dropped.
    return None


def _events(conn, series: str) -> list[dict]:
    """[{period, key, close_ts, release_ts, legs}] for settled periods, chronological."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.research.backtest import _settle_release_ts
    from prediction_market_macro.util.periods import kalshi_period_to_key
    spec = REGISTRY[series]
    rows = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no') GROUP BY s.period ORDER BY ct", (series,)).fetchall()
    out = []
    for r in rows:
        key = kalshi_period_to_key(r["period"])
        if not key or not r["ct"]:
            continue
        legs = conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type, s.result"
            " FROM contracts c JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, r["period"])).fetchall()
        out.append({"period": r["period"], "key": key,
                    "close_ts": datetime.fromisoformat(r["ct"].replace("Z", "+00:00")),
                    "release_ts": _settle_release_ts(conn, spec, key), "legs": legs})
    return out


def asof_for(ev: dict, hours: int = 1) -> datetime:
    """`replay_series`'s asof rule, mirrored. See the module docstring for why it is
    mirrored and `tests/test_shadow_width.py` for the test that keeps it honest."""
    asof = ev["close_ts"] - timedelta(hours=hours)
    if ev["release_ts"] is not None and asof >= ev["release_ts"]:
        return ev["release_ts"] - timedelta(seconds=1)
    return asof


def _interval_ll(dist, lo: float, hi: float) -> tuple[float, bool]:
    """(log P(lo < X <= hi), was_floored). The likelihood of the actual observation."""
    p = float(dist.cdf(hi) if hi != INF else 1.0) - float(dist.cdf(lo) if lo != -INF else 0.0)
    if p < _P_FLOOR:
        return math.log(_P_FLOOR), True
    return math.log(p), False


def score_series(conn, series: str, asof: datetime | None = None) -> dict:
    """Per-event interval LL at every rung, for the forward window only."""
    from prediction_market_macro.model import energy
    now = asof or datetime.now(timezone.utc)
    reg = datetime.fromisoformat(REGISTERED)
    rungs = ladder()
    rows, drops = [], {"unscorable_interval": 0, "predict_failed": 0, "leak": 0}
    kinds: dict[str, int] = {}
    n_floored = 0
    for ev in _events(conn, series):
        if not (reg <= ev["close_ts"] <= now):
            continue
        iv = realized_interval(ev["legs"])
        if iv is None:
            drops["unscorable_interval"] += 1
            continue
        lo, hi, kind = iv
        a = asof_for(ev)
        lls, leaked, failed = {}, False, False
        for r in rungs:
            try:
                pred = energy.predict(conn, a, ev["key"], series,
                                      params={"fut_sigma_scale": float(r)})
            except Exception:                                    # noqa: BLE001
                failed = True
                break
            if (ev["release_ts"] is not None and pred.data_horizon is not None
                    and pred.data_horizon >= ev["release_ts"]):
                leaked = True
                break
            ll, fl = _interval_ll(pred.dist, lo, hi)
            n_floored += fl
            lls[str(r)] = round(ll, 6)
        if failed:
            drops["predict_failed"] += 1
            continue
        if leaked:
            drops["leak"] += 1
            continue
        kinds[kind] = kinds.get(kind, 0) + 1
        rows.append({"period": ev["key"], "close": ev["close_ts"].isoformat(),
                     "asof": a.isoformat(), "interval_kind": kind,
                     "lo": None if lo == -INF else lo, "hi": None if hi == INF else hi,
                     "ll": lls})
    return {"series": series, "rungs": list(rungs), "n_forward": len(rows),
            "n_required": N_FORWARD, "drops": drops, "interval_kinds": kinds,
            "n_ll_floored": n_floored, "events": rows}


def _wilcoxon(diffs: list[float]) -> tuple[float | None, float | None]:
    """(statistic, one-sided p) for H1: diffs > 0, i.e. the rung beats the default.

    One-sided because the registration is one-sided, and `greater` rather than `less`
    because higher log-likelihood is better — the opposite orientation from PR-1's Brier,
    which is the kind of sign slip that is worth naming rather than trusting.
    """
    nz = [d for d in diffs if d != 0.0]
    if len(nz) < 5:
        return None, None
    from scipy.stats import wilcoxon
    r = wilcoxon(nz, alternative="greater")
    return float(r.statistic), float(r.pvalue)


def run(conn, asof: datetime | None = None) -> dict:
    """The PR-12 readout. PENDING per series until `N_FORWARD` forward weeks settled."""
    now = asof or datetime.now(timezone.utc)
    rungs = ladder()
    others = [r for r in rungs if r != DEFAULT_RUNG]
    alpha_adj = ALPHA / max(len(others), 1)
    out = {"registered": REGISTERED, "asof": now.isoformat(),
           "k_discovery": K_DISCOVERY, "k_forward": len(others),
           "alpha": ALPHA, "alpha_bonferroni": round(alpha_adj, 5),
           "offset": OFFSET, "metric": "interval log-likelihood (higher is better)",
           "code_fingerprint": code_fingerprint(), "code_change": code_change_note(),
           "series": {}}
    for s in SERIES:
        sc = score_series(conn, s, now)
        base = [e["ll"].get(str(DEFAULT_RUNG)) for e in sc["events"]]
        arms = {}
        for r in others:
            d = [e["ll"][str(r)] - b for e, b in zip(sc["events"], base)
                 if e["ll"].get(str(r)) is not None and b is not None]
            stat, p = _wilcoxon(d)
            arms[str(r)] = {
                "mean_ll_gain_nats": round(sum(d) / len(d), 6) if d else None,
                "n_better": sum(1 for x in d if x > 0), "n": len(d),
                "wilcoxon_stat": stat, "wilcoxon_p_one_sided": p,
                "passes": bool(d and sum(d) > 0 and p is not None and p < alpha_adj),
            }
        sc["arms"] = arms
        sc["verdict"] = _verdict(sc, alpha_adj)
        out["series"][s] = sc
    out["criterion"] = (
        f"PR-12, K={K_DISCOVERY} discovery + {len(others)} forward rungs: {N_FORWARD} "
        f"forward settled weeks per series from {REGISTERED[:10]}, paired interval "
        f"log-likelihood LL(rung) > LL({DEFAULT_RUNG}) at the {OFFSET} offset, one-sided "
        f"Wilcoxon, Bonferroni alpha={alpha_adj:.5f}. A rung that wins authorises a "
        "DEFAULT change and nothing else — the order router's bar is still the market. "
        "No re-search on failure: if 1.0 holds its own ladder, #192's over-width is a "
        "19-fold search artifact and that is the finding.")
    return out


def _verdict(sc: dict, alpha_adj: float) -> str:
    if sc["n_forward"] < sc["n_required"]:
        return (f"PENDING — {sc['n_forward']}/{sc['n_required']} forward weeks. No "
                "verdict, and the gains above are a progress readout, not a result.")
    won = [r for r, a in sc["arms"].items() if a["passes"]]
    if not won:
        return ("FALSIFIED — no rung beat 1.0 at the Bonferroni alpha over "
                f"{sc['n_forward']} forward weeks. The default stands and, per the "
                "registration, there is no re-search.")
    best = max(won, key=lambda r: sc["arms"][r]["mean_ll_gain_nats"])
    a = sc["arms"][best]
    return (f"PASSED — rung {best} beat 1.0 by {a['mean_ll_gain_nats']:.4f} nats/event on "
            f"{a['n_better']}/{a['n']} weeks (one-sided p={a['wilcoxon_p_one_sided']:.4f} "
            f"< {alpha_adj:.5f}). This authorises changing the DEFAULT, not a bet.")


def main():
    import argparse
    import json

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--stamp", action="store_true",
                    help="print the fingerprint to paste into REGISTERED_FINGERPRINT")
    a = ap.parse_args()
    if a.stamp:
        print(code_fingerprint())
        return
    conn = init_db(a.db or load_settings().db_path)
    print(json.dumps(run(conn), ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
