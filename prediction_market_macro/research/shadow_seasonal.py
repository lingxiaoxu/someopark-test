"""research/shadow_seasonal.py — PR-11 (#197): grade claims/0.2.0's screened seasonal centre.

`docs/PREREGISTER.md` PR-11 registered ONE hypothesis on 2026-08-27 and this module is the
only thing allowed to grade it. It lands in the same commit as the change it grades,
because PR-8 and PR-10 both registered a criterion whose scorer was still unwritten weeks
later (#195) and a criterion nothing computes is not a criterion.

The registration, verbatim
--------------------------
    改动   model/claims.py 0.1.0 -> 0.2.0, seasonal_estimator="mad_screen:10"
    判据   前向 >=6 个开火事件, T-1h 成对区间对数似然:
           dLL 均值 > 0 且 >=4/6 逐事件为正。做不到即证伪, 退回 mean
    K      10

Three things in that text drive the whole design.

**"开火事件" — only the events the screen touches.** The screen changes ISO weeks
12/13/14/15/18 and nothing else, so on ~48 of 53 weeks the two arms are the SAME NUMBER
and dLL is exactly 0.0. Averaging those in would let a criterion pass on arithmetic: eight
zeros and one small win is a positive mean and eight-of-nine non-negative. So an event
counts only when the two arms actually disagree, and firing is decided by comparing the
arms' own `seasonal` input rather than by a hardcoded week list — a week list would be a
constant that could drift away from what the code does, and the point of #197 is that a
seasonal estimate is a function of the data, not of a calendar.

The honest cost is stated in the registration: ~5 firing weeks a year means 6 of them is
about 15 months. `verdict()` says PENDING until then and does not print a p-value.

**Interval log-likelihood, not Brier.** Brier needs T-1h quotes on the settled legs, and
KXJOBLESSCLAIMS has them on some events and not others; a criterion that silently scores a
quote-covered subset is choosing its own sample. The probability the settling grid cell
gets is defined for every settled event, so the sample is the whole forward window.

**Both arms replayed by the same code at the same asof.** Both go through `_asof_for`,
which is `backtest.replay_series`'s rule — step back behind the print when the book closed
after it, drop an event whose `data_horizon` reached past the release. A paired test whose
two arms sat at different asofs is not a paired test, and the baseline arm is reconstructed
by PASSING `seasonal_estimator="mean"` to today's code rather than by importing an old
copy, so nothing but the estimator differs between them.

What the actual print is
------------------------
Not from `fred_obs`: that is the same store the model reads, and a label taken from the
model's own input door is one PIT mistake away from grading the model on its own inputs.
It comes from the settled ladder instead. Every `greater_or_equal` leg that settled `yes`
puts the print at or above its strike and every `no` leg puts it below, so the print is
bracketed to `[max(yes), min(no))` — on a 5,000-wide ladder against a sigma of 7k-18k that
is a fraction of a sigma, and it is the same construction the discovery used.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
from datetime import datetime, timedelta, timezone

# The registration. None of these may be edited to make a result pass — that is the whole
# point of the file they came from.
REGISTERED = "2026-08-27T00:00:00+00:00"   # forward count starts at this settlement
SERIES = "KXJOBLESSCLAIMS"
BASELINE = {"seasonal_estimator": "mean"}          # claims/0.1.0's centre
CANDIDATE = {"seasonal_estimator": "mad_screen:10"}  # claims/0.2.0's, == the default
N_FIRING = 6                               # registered sample size, in FIRING events
MIN_POSITIVE = 4                           # registered per-event floor, out of N_FIRING
OFFSET = "-1h"                             # the offset the registration's evidence used
K = 10                                     # arms tried on this data, per the registration
GRID = 250.0                               # KXJOBLESSCLAIMS ladder resolution


def code_fingerprint() -> str:
    """sha1 prefix of `model/claims.py`, for the same reason `shadow_claims` records one:
    a mid-flight model change moves both arms together, so the pairing survives but the
    registered effect size does not, and that has to be visible rather than inferred."""
    p = pathlib.Path(__file__).resolve().parent.parent / "model" / "claims.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def settled_brackets(conn, series: str = SERIES) -> dict[str, dict]:
    """{kalshi period token -> {lo, hi, close}} from the settled >= ladder.

    An event with no settled `yes` or no settled `no` is not bracketed — the print was
    off the end of the ladder — and is dropped rather than guessed at.
    """
    rows = conn.execute(
        "SELECT c.period, c.floor_strike, c.close_time, s.result FROM contracts c"
        " JOIN settlements s ON s.ticker=c.ticker WHERE c.series=?"
        " AND s.result IN ('yes','no') AND c.strike_type='greater_or_equal'"
        " AND c.floor_strike IS NOT NULL", (series,)).fetchall()
    acc: dict[str, dict] = {}
    for r in rows:
        d = acc.setdefault(r["period"], {"yes": [], "no": [], "close": r["close_time"]})
        d[r["result"]].append(float(r["floor_strike"]))
        if r["close_time"] and r["close_time"] > (d["close"] or ""):
            d["close"] = r["close_time"]
    out = {}
    for token, d in acc.items():
        if not d["yes"] or not d["no"] or not d["close"]:
            continue
        lo, hi = max(d["yes"]), min(d["no"])
        if hi <= lo:                        # contradictory settlements; not our business
            continue
        out[token] = {"lo": lo, "hi": hi,
                      "close": datetime.fromisoformat(d["close"].replace("Z", "+00:00"))}
    return out


def _asof_for(conn, spec, key: str, close_ts: datetime) -> tuple[datetime, datetime | None]:
    """`replay_series`'s asof rule, reused rather than re-derived."""
    from prediction_market_macro.research.backtest import _settle_release_ts
    release_ts = _settle_release_ts(conn, spec, key)
    asof = close_ts - timedelta(hours=1)
    if release_ts is not None and asof >= release_ts:
        asof = release_ts - timedelta(seconds=1)
    return asof, release_ts


MASS_FLOOR = 1e-12


def _interval_ll(pred, actual: float) -> float:
    """log P(the grid cell the print settled in). `grid_pmf` is the same discretisation
    `leg_fair` prices off, so this is the model's own probability, not a re-derivation.

    The mass is floored, and an empty grid returns that floor rather than -inf. An
    unbounded penalty is not a measurement: one event whose print landed far off the
    ladder would swamp a six-event mean and decide the registration by itself, in
    whichever direction the arm that happened to be narrower fell. Both arms hit the same
    floor, so a genuine blow-up still costs the narrower arm — it just cannot cost it
    infinitely.
    """
    from prediction_market_macro.model.common import grid_pmf
    pmf = grid_pmf(pred.dist, GRID)
    if not pmf:
        return math.log(MASS_FLOOR)
    cell = min(pmf, key=lambda k: abs(k - actual))
    return math.log(max(pmf.get(cell, 0.0), MASS_FLOOR))


def run(conn, now: datetime | None = None) -> dict:
    """Both arms on every settled event since the registration. Grades nothing by itself —
    `verdict` does that, and only once the registered firing count is reached."""
    import importlib

    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    from prediction_market_macro.util.periods import kalshi_period_to_key
    now = now or datetime.now(timezone.utc)
    reg = datetime.fromisoformat(REGISTERED)
    spec = REGISTRY[SERIES]
    disp = SERIES_DISPATCH[SERIES]
    fn = getattr(importlib.import_module(disp[0]), disp[1])

    events, dropped = [], []
    for token, b in sorted(settled_brackets(conn).items(), key=lambda kv: kv[1]["close"]):
        if b["close"] <= reg:
            continue                         # settled before the registration; not forward
        key = kalshi_period_to_key(token)
        if not key:
            dropped.append({"period": token, "why": "unmappable period token"})
            continue
        asof, release_ts = _asof_for(conn, spec, key, b["close"])
        arms = {}
        for name, params in (("base", BASELINE), ("cand", CANDIDATE)):
            try:
                arms[name] = fn(conn, asof, key, series=SERIES, params=dict(params))
            except Exception as e:            # noqa: BLE001
                dropped.append({"period": token, "why": f"{name} arm raised: {e!r}"})
                arms = None
                break
        if arms is None:
            continue
        # the same leak guard replay_series applies: an input whose vintage read is not
        # asof-bounded can reach past the print no matter where asof sits.
        if release_ts is not None and any(
                a.data_horizon is not None and a.data_horizon >= release_ts
                for a in arms.values()):
            dropped.append({"period": token, "why": "data_horizon reached past the release"})
            continue
        actual = 0.5 * (b["lo"] + b["hi"])
        s_base = arms["base"].inputs["seasonal"]
        s_cand = arms["cand"].inputs["seasonal"]
        ll_b, ll_c = _interval_ll(arms["base"], actual), _interval_ll(arms["cand"], actual)
        events.append({
            "period": key, "close": b["close"].isoformat(),
            "iso_week": arms["base"].inputs["target_week"],
            "bracket": [b["lo"], b["hi"]],
            "fired": abs(s_cand - s_base) > 1e-9,
            "seasonal_base": s_base, "seasonal_cand": s_cand,
            "mu_base": round(arms["base"].dist.comps[0][1], 1),
            "mu_cand": round(arms["cand"].dist.comps[0][1], 1),
            "ll_base": round(ll_b, 6), "ll_cand": round(ll_c, 6),
            "dll": round(ll_c - ll_b, 6),
        })

    firing = [e for e in events if e["fired"]]
    out = {
        "registered": REGISTERED, "k": K, "series": SERIES,
        "arms": {"base": BASELINE, "cand": CANDIDATE},
        "offset": OFFSET, "asof": now.isoformat(),
        "code_fingerprint": code_fingerprint(),
        "n_settled_since_registration": len(events),
        "n_firing": len(firing), "n_required_firing": N_FIRING,
        "dropped": dropped,
        "events": events,
    }
    if firing:
        d = [e["dll"] for e in firing]
        out["primary"] = {
            "comparison": "candidate vs baseline interval LL, FIRING events only"
                          " (REGISTERED)",
            "mean_dll": round(sum(d) / len(d), 6),
            "n_positive": sum(x > 0 for x in d),
            "n_required_positive": MIN_POSITIVE,
        }
    # reported because a reader will ask, and refused as evidence because the registration
    # says the mean is taken over firing events: every non-firing event contributes exactly
    # 0.0 and would move the mean toward 0 without carrying any information about the screen.
    if events:
        d_all = [e["dll"] for e in events]
        out["secondary"] = {
            "comparison": "all settled events (DILUTED — grades nothing; every non-firing"
                          " event is exactly 0.0 by construction)",
            "mean_dll": round(sum(d_all) / len(d_all), 6),
            "n_zero": sum(abs(x) < 1e-12 for x in d_all),
        }
    out["criterion"] = (
        f"PR-11, K={K}: {N_FIRING} forward FIRING events from {REGISTERED[:10]}, paired"
        f" interval log-likelihood at {OFFSET}. Pass = mean dLL > 0 AND at least"
        f" {MIN_POSITIVE}/{N_FIRING} individually positive. Failing it falsifies the"
        f" screened centre and claims reverts to seasonal_estimator='mean'. No retry with"
        f" a different k, a different estimator or a different metric — the registration"
        f" names all three as forbidden, which is why K is 10 and not unbounded.")
    out["verdict"] = verdict(out)
    return out


def verdict(rep: dict) -> str:
    n = rep["n_firing"]
    if n < N_FIRING:
        return (f"PENDING — {n}/{N_FIRING} forward firing events. No verdict, and the"
                f" numbers above are a progress readout, not a result. The screen fires on"
                f" ~5 ISO weeks a year, so this is expected to take about 15 months; that"
                f" was registered in advance and is not a reason to shorten the window.")
    p = rep["primary"]
    if p["mean_dll"] > 0 and p["n_positive"] >= MIN_POSITIVE:
        return (f"PASS — mean dLL {p['mean_dll']:+.4f} over {n} firing events,"
                f" {p['n_positive']}/{n} positive.")
    return (f"FALSIFIED — mean dLL {p['mean_dll']:+.4f}, {p['n_positive']}/{n} positive;"
            f" the registration says claims reverts to seasonal_estimator='mean'.")


def main():
    import sqlite3

    from prediction_market_macro.config.settings import load_settings
    conn = sqlite3.connect(load_settings().db_path)
    conn.row_factory = sqlite3.Row
    print(json.dumps(run(conn), indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
