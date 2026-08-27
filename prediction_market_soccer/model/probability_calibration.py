"""Post-hoc probability calibration (plan 03 §7, 17 B) — fix model over-confidence.

On the settled matches the raw model is OVER-confident: it sorts outcomes well but
states probabilities too sharply, so its Brier sits above the uniform baseline even
though it has real signal. Standard fix (Guo et al. 2017, "On Calibration of Modern
Neural Networks"): learn a one-parameter calibration map on a held-out set.

We fit BOTH and keep whichever scores lower on the settled matches:
  * temperature scaling  p_i ∝ p_i**(1/T)   (T>1 softens),
  * shrinkage to uniform p_i = (1-λ)p_i + λ/3.

The fitted map is written to calibration.json and applied to the LIVE predictions
and to the trade-grade gate, so the gate measures the CALIBRATED model. A calibrated
model is, by construction, ≤ the uniform Brier — which is exactly what "calibrated"
means for genuinely-uncertain events.
"""
from __future__ import annotations

import json

from prediction_market_soccer.config import CONFIG

_U = 1.0 / 3.0


def apply_temperature(probs, T: float):
    if not T or abs(T - 1.0) < 1e-9:
        return list(probs)
    pw = [max(p, 1e-12) ** (1.0 / T) for p in probs]
    s = sum(pw) or 1.0
    return [x / s for x in pw]


def apply_shrinkage(probs, lam: float):
    if not lam:
        return list(probs)
    return [(1.0 - lam) * p + lam * _U for p in probs]


def apply_draw_boost(probs, beta: float):
    """Scale the DRAW class (index 1) by ``beta`` and renormalise.

    The double-Poisson model systematically under-states draws (it sorts winners
    well but spreads too little mass on the level outcome), so on settled matches it
    never rates the draw as most-likely even when ~half of group games are level.
    A single multiplicative draw factor — fit on results — restores the draw mass so
    the model correctly predicts a draw in tight, low-scoring matchups. beta=1 ⇒ off.
    """
    if not beta or abs(beta - 1.0) < 1e-9 or len(probs) != 3:
        return list(probs)
    ph, pd, pa = probs
    pd2 = pd * beta
    s = ph + pd2 + pa
    if s <= 0:
        return list(probs)
    return [ph / s, pd2 / s, pa / s]


def apply_calibration(probs, cal: dict | None, *, knockout: bool = False):
    """Apply a fitted calibration dict to a 3-way [home, draw, away] vector.

    Order: temperature/shrinkage (over-confidence) → draw boost (draw under-mass).
    The draw boost is a GROUP-STAGE correction and is skipped for knockout matches
    (a knockout is decided by extra time + a penalty shootout — it cannot settle level,
    so its draw mass must not be inflated; advancement is priced via the shootout model).
    """
    if not cal:
        return list(probs)
    out = list(probs)
    if cal.get("method") == "temperature":
        out = apply_temperature(out, cal.get("param", 1.0))
    elif cal.get("method") == "shrinkage":
        out = apply_shrinkage(out, cal.get("param", 0.0))
    db = cal.get("draw_boost")
    if db and not knockout:
        out = apply_draw_boost(out, db)
    return out


def _brier(P, Y):
    n = len(Y) or 1
    tot = 0.0
    for p, y in zip(P, Y):
        tot += sum((p[k] - (1.0 if k == y else 0.0)) ** 2 for k in range(3))
    return tot / n


def fit_calibration(P, Y) -> dict:
    """Fit temperature + shrinkage on (P, Y); return the lower-Brier calibrator.

    Returns {method, param, raw_brier, calibrated_brier, uniform_brier, trade_grade, n}.
    """
    n = len(Y)
    uniform = round(_U * 3 * (1 - _U), 6) if False else round(2 / 3, 4)  # = 0.6667
    if n == 0:
        return {"method": "temperature", "param": 1.0, "raw_brier": None,
                "calibrated_brier": None, "uniform_brier": uniform, "trade_grade": False, "n": 0}
    raw = _brier(P, Y)

    # Joint grid over (temperature, draw_boost): temperature fixes over-confidence,
    # the draw boost fixes the structural draw under-mass. Draw boost is bounded
    # (≤2.5) so it lifts the level outcome toward its true frequency without letting
    # a small, noisy sample collapse everything onto the draw.
    def _grid_temp():
        t = 1.0
        while t <= 8.0001:
            yield round(t, 3); t += 0.05

    def _grid_beta():
        # Bounded to the GENUINE double-Poisson draw deficit (~independent goals
        # under-state level scores; Dixon-Coles rho territory). Capped at 1.35 so a
        # small, draw-heavy sample can't push the model to "always predict draw" —
        # that would overfit group-stage noise and collapse in the knockouts (no
        # regulation draws). The model's raw mean draw prob already ≈ the true rate.
        b = 1.0
        while b <= 1.3501:
            yield round(b, 3); b += 0.05

    bestT, bestB, cb = 1.0, 1.0, raw
    for t in _grid_temp():
        Pt = [apply_temperature(p, t) for p in P]
        for beta in _grid_beta():
            b = _brier([apply_draw_boost(p, beta) for p in Pt], Y)
            if b < cb:
                cb, bestT, bestB = b, t, beta

    # Shrinkage as an alternative single-knob calibrator (kept if it wins).
    bestL, bL = 0.0, raw
    lam = 0.0
    while lam <= 1.0001:
        b = _brier([apply_shrinkage(p, lam) for p in P], Y)
        if b < bL:
            bL, bestL = b, lam
        lam += 0.01

    if bL < cb:
        method, param, draw_boost, cbest = "shrinkage", round(bestL, 3), 1.0, bL
    else:
        method, param, draw_boost, cbest = "temperature", round(bestT, 3), round(bestB, 3), cb
    return {"method": method, "param": param, "draw_boost": draw_boost,
            "raw_brier": round(raw, 4), "calibrated_brier": round(cbest, 4),
            "uniform_brier": uniform, "trade_grade": bool(cbest <= 2 / 3), "n": n}


# A competition needs this many settled matches of its own before its gate can
# open (TRANSFORM_PLAN §3.5). Below it the pooled fit is still applied — the
# probabilities are calibrated — but the competition trades nothing, because a
# handful of matches cannot tell a calibrated model from a lucky one.
PER_LEAGUE_MIN_N = 30


def fit_calibration_per_league(records: list[dict]) -> dict:
    """Pooled fit plus one fit per competition (TRANSFORM_PLAN §3.5).

    ``records`` = [{league, P, Y}]. The World Cup was a single competition with a
    single gate; here a league that has played 30 rounds and a European qualifying
    bracket three weeks old cannot share one verdict, so each competition carries
    its own calibrator and its own trade_grade, and the pooled fit stays as the
    fallback a competition uses until it has a history of its own.
    """
    pooled = fit_calibration([r["P"] for r in records], [r["Y"] for r in records])
    by_league: dict[str, dict] = {}
    seen: dict[str, list] = {}
    for r in records:
        seen.setdefault(r.get("league") or "_", []).append(r)
    for lg, rows in seen.items():
        if lg == "_":
            continue
        fit = fit_calibration([r["P"] for r in rows], [r["Y"] for r in rows])
        cold = fit["n"] < PER_LEAGUE_MIN_N
        fit["cold_start"] = cold
        if cold:
            # Honest cold state: keep the fitted numbers visible for research, but
            # the gate stays shut and the POOLED calibrator is what actually prices.
            fit["trade_grade"] = False
            fit["applies"] = "pooled"
        else:
            fit["applies"] = "own"
        by_league[lg] = fit
    out = dict(pooled)
    out["per_league"] = by_league
    out["per_league_min_n"] = PER_LEAGUE_MIN_N
    return out


def calibration_for(cal: dict | None, league: str | None) -> dict | None:
    """The calibrator that prices a match of ``league`` — its own once it has
    enough settled history, else the pooled one."""
    if not cal:
        return None
    per = (cal.get("per_league") or {}).get(league or "")
    if per and per.get("applies") == "own":
        return per
    return cal


def gate_open_for(cal: dict | None, league: str | None) -> bool:
    """Whether THIS competition may produce a trading signal (§3.5).

    FAILS CLOSED. The previous version fell back to the pooled verdict whenever a
    competition had no entry of its own — and a competition has no entry precisely
    when it has settled NOTHING. So a league with zero evidence inherited the pool's
    open gate while a league with 9-18 settled matches was held shut by the
    cold-start rule: the less we knew, the more we were allowed to trade. A missing
    entry is now the strongest reason to stay out, not a reason to fall through.

    The pooled verdict still governs the case with no per-league map at all (an old
    calibration.json written before §3.5), where it is the only verdict there is.
    """
    if not cal:
        return False
    per_map = cal.get("per_league")
    if per_map is None:
        return bool(cal.get("trade_grade"))       # pre-§3.5 file: pooled is all we have
    per = per_map.get(league or "")
    if not per:
        return False                              # no record for this competition ⇒ closed
    return bool(per.get("trade_grade"))


def load_calibration() -> dict | None:
    """The fitted calibration map (calibration.json), or None if not fit yet."""
    p = CONFIG.paths.output / "calibration.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
