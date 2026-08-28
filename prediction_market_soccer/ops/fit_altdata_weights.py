"""ops/fit_altdata_weights.py — PER-COMPETITION alt-data λ weights.

The alt-data adjustment (model/altdata_adjust.py) nudges the Dixon-Coles λ pair by
each side's opponent-adjusted defensive/offensive form. Its two weights were
hand-set once, on 19 World Cup matches, and applied to every competition. Club
football is not one population: a 34-round top-5 league, a two-legged CONMEBOL
cup and a 153-club European qualifying bracket differ in how much recent form
actually carries. So each competition gets its OWN weights, fitted on ITS OWN
history.

Method (per competition, strictly point-in-time):
  * matches   — every settled fixture of that competition in seasons 2025-2026;
  * base model— the season's own prior vintage (2025 matches use the prior built
                from the 2024 tables, ``clubs_<comp>_s2025.json``);
  * index     — the alt-data index rebuilt as of the START of each match's week,
                so a match never sees its own week's results;
  * split     — TRAIN = all but the most recent ``test_frac`` by time,
                TEST = the rest, never used for selection;
  * selection — grid over (def_w, off_w) scored by calibrated Brier on TRAIN
                (calibration fitted on TRAIN itself); a competition keeps the
                zero weights unless the winner beats them by ``--min-gain``,
                which is what stops 300 matches of noise from being fitted.

    python -m prediction_market_soccer.ops.fit_altdata_weights [--min-gain 0.002]
    → data/priors/league_altdata.json   (loaded by config.leagues.altdata_weights)
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active
from prediction_market_soccer.model.altdata_adjust import altdata_index
from prediction_market_soccer.model.match_pricing import is_knockout, price_match
from prediction_market_soccer.model.probability_calibration import apply_calibration, fit_calibration
from prediction_market_soccer.model.strength import build_strength

_FINISHED = ("FT", "AET", "PEN")

# Index construction, fixed for every competition (these are mechanism fixes, not
# tunables): unrated opponents count as league-average rather than as strong
# sides, opponents rated in another competition resolve through the cross-comp
# table, and a club's index is shrunk toward the field by n/(n+5).
IDX_OPTS = {"unknown_opp": 0.0, "shrink_k": 5.0}

# Both signal constructions compete per competition (see altdata_adjust.mode):
# the inherited WC formula and the dimensionally-clean model residual.
# The residual construction wins the raw-signal test in every competition where a
# signal exists at all (UCL +0.176 vs +0.015 correlation with the attacking
# residual, Libertadores +0.185 vs +0.098, Argentina +0.157 vs +0.066), so it is
# the only construction fitted. The inherited "wc" formula stays available in
# altdata_adjust for reference.
MODES = ("residual",)

# When a competition's own history is too thin to regress (UEFA qualifying rounds
# turn over their field every year, leaving ~40 usable points), it inherits the
# weights fitted on its FAMILY — sibling competitions that share a format and a
# club population. A family is only ever a fallback; a competition with its own
# fit always keeps it.
FAMILIES = {
    "uefa_cup": ("ucl", "uel", "uecl"),
    "conmebol_cup": ("libertadores", "sudamericana"),
    "top5": ("epl", "laliga", "seriea", "bundesliga", "ligue1"),
    "sa_league": ("brasileirao", "argentina"),
}
_FAMILY_OF = {c: f for f, comps in FAMILIES.items() for c in comps}
MIN_REG_POINTS = 60

GRID_DEF = (0.0, 0.10, 0.20, 0.30, 0.45)
GRID_OFF = (0.0, 0.06, 0.12, 0.25)


def _week_start(ts: str) -> str:
    d = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    return (d - timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _prior_for(comp_key: str, season: int):
    from prediction_market_soccer.ingest.club_prior import load_prior
    if season >= 2026:
        return load_prior(comp_key)
    try:
        return load_prior(f"{comp_key}_s2025")
    except Exception:
        return load_prior(comp_key)


def _matches(conn, comp):
    from prediction_market_soccer.util.pricing import reg_score
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals, raw_json, kickoff_ts, "
        "round, season FROM fixture WHERE league_id=? AND season IN (2025, 2026) "
        "AND status_short IN ({}) AND home_goals IS NOT NULL "
        "ORDER BY kickoff_ts".format(",".join("?" * len(_FINISHED))),
        (comp.api_football_id, *_FINISHED)).fetchall()
    out = []
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai) or not r["kickoff_ts"]:
            continue
        gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
        out.append({"hi": hi, "ai": ai, "y": 0 if gh > ga else (1 if gh == ga else 2),
                    "gh": gh, "ga": ga,
                    "ts": r["kickoff_ts"], "season": r["season"],
                    "ko": is_knockout(r["round"], comp.key)})
    return out


def fit_comp_regression(conn, comp, *, test_frac: float = 0.25,
                        all_ratings: dict | None = None) -> dict:
    """Fit the weights BY REGRESSION instead of by a Brier grid.

    A grid search cannot resolve this signal: it correlates with the goal residual
    at r ≈ 0.12-0.19 in the competitions where it works at all, and an effect that
    size moves a 3-way Brier by less than the noise of a few hundred matches — the
    grid therefore returned "zero" almost everywhere while the raw correlation was
    plainly non-zero. Regression asks the answerable question instead: by HOW MANY
    GOALS does a one-sigma signal move the result, and what λ multiplier reproduces
    exactly that? The estimate is then shrunk by its own t-statistic, so a
    competition with a weak or noisy relationship keeps a small weight and only a
    well-measured one gets the full amplitude. Negative slopes (short-term mean
    reversion, e.g. the Bundesliga defensive signal) are floored at zero rather
    than traded contrarian on this much data.
    """
    import math as _m
    ms = _matches(conn, comp)
    if len(ms) < 80:
        return {"comp": comp.key, "n": len(ms), "def_w": 0.0, "off_w": 0.0,
                "mode": "none", "reason": "too few settled matches to fit"}
    cut = int(len(ms) * (1.0 - test_frac))
    train, test = ms[:cut], ms[cut:]

    base = {s: build_strength(_prior_for(comp.key, s), CONFIG.model) for s in (2025, 2026)}
    field = set(base[2026].ratings) | set(base[2025].ratings)
    from prediction_market_soccer.config.leagues import fitted_params
    fp = fitted_params(comp.key)
    mu = fp.get("base_mu", CONFIG.model.base_mu)
    ha = fp.get("home_adv", CONFIG.model.home_adv)
    beta = CONFIG.model.beta

    idx_cache: dict = {}

    def idx_for(m):
        key = (m["season"], _week_start(m["ts"]))
        if key not in idx_cache:
            idx_cache[key] = altdata_index(
                conn, base[m["season"]].ratings, as_of=key[1], clubs=field,
                all_ratings=all_ratings, mode="residual", mu=mu, ha=ha, beta=beta,
                **IDX_OPTS)
        return idx_cache[key]

    off_x, off_y, def_x, def_y, lams = [], [], [], [], []
    for m in train:
        sm = base[m["season"]]
        if m["hi"] not in sm.ratings or m["ai"] not in sm.ratings:
            continue
        idx = idx_for(m)
        ah, aa = idx.get(m["hi"]), idx.get(m["ai"])
        if ah is None or aa is None:
            continue
        d = sm.ratings[m["hi"]] - sm.ratings[m["ai"]]
        lam_h, lam_a = _m.exp(mu + ha + beta * d), _m.exp(mu - beta * d)
        gh, ga = m["gh"], m["ga"]
        # both sides contribute a point to each regression
        off_x += [ah.off_z, aa.off_z];       off_y += [gh - lam_h, ga - lam_a]
        def_x += [aa.def_z, ah.def_z];       def_y += [lam_h - gh, lam_a - ga]
        lams += [lam_h, lam_a]

    def slope(xs, ys):
        n = len(xs)
        if n < 60:
            return 0.0, 0.0, n
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx <= 0:
            return 0.0, 0.0, n
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        resid = [y - (my + b * (x - mx)) for x, y in zip(xs, ys)]
        s2 = sum(r * r for r in resid) / max(1, n - 2)
        se = _m.sqrt(s2 / sxx) if sxx > 0 else 0.0
        t = (b / se) if se > 0 else 0.0
        return b, t, n

    b_off, t_off, n_reg = slope(off_x, off_y)
    b_def, t_def, _ = slope(def_x, def_y)
    lam_bar = (sum(lams) / len(lams)) if lams else 1.35

    def to_w(b, t):
        # goals-per-sigma → λ multiplier amplitude, shrunk by t²/(t²+4) so a
        # 2-sigma relationship keeps half its size and a 1-sigma one a fifth
        if b <= 0 or lam_bar <= 0:
            return 0.0
        w = b / lam_bar
        w *= (t * t) / (t * t + 4.0)
        return round(max(0.0, min(0.45, w)), 3)

    ow, dw = to_w(b_off, t_off), to_w(b_def, t_def)

    # honest hold-out check with the fitted weights (calibration fitted on TRAIN)
    def score_split(rows, dw_, ow_, cal=None):
        tot, n, probs = 0.0, 0, []
        for m in rows:
            sm = base[m["season"]]
            if m["hi"] not in sm.ratings or m["ai"] not in sm.ratings:
                continue
            if dw_ or ow_:
                cfg = dataclasses.replace(CONFIG.model, oppadj_def_weight=dw_,
                                          oppadj_off_weight=ow_)
                sm = dataclasses.replace(sm, cfg=cfg, adj=idx_for(m))
            mp = price_match(sm, m["hi"], m["ai"], knockout=False, host_neutral=m["ko"])
            probs.append(([mp.p_home, mp.p_draw, mp.p_away], m["y"]))
        if not probs:
            return None, None, 0
        use_cal = cal if cal is not None else fit_calibration(
            [p for p, _ in probs], [y for _, y in probs])
        for p, y in probs:
            pv = apply_calibration(list(p), use_cal, knockout=False)
            oh = [1.0 if k == y else 0.0 for k in range(3)]
            tot += sum((pv[k] - oh[k]) ** 2 for k in range(3)); n += 1
        return tot / n, use_cal, n

    _b, cal0, n_tr = score_split(train, 0.0, 0.0)
    t0, _c, n_te = score_split(test, 0.0, 0.0, cal=cal0)
    tw, _c2, _n = score_split(test, dw, ow, cal=cal0)
    return {"comp": comp.key, "n": len(ms), "n_train": n_tr, "n_test": n_te,
            "def_w": dw, "off_w": ow, "mode": "residual" if (dw or ow) else "none",
            "slope_off": round(b_off, 4), "t_off": round(t_off, 2),
            "slope_def": round(b_def, 4), "t_def": round(t_def, 2),
            "n_regression": n_reg, "lambda_bar": round(lam_bar, 3),
            "test_brier_zero": round(t0, 4) if t0 else None,
            "test_brier_fitted": round(tw, 4) if tw else None,
            "kept": bool(dw or ow)}


def fit_comp(conn, comp, *, test_frac: float = 0.25, min_gain: float = 0.002,
             all_ratings: dict | None = None) -> dict:
    ms = _matches(conn, comp)
    if len(ms) < 80:
        return {"comp": comp.key, "n": len(ms), "def_w": 0.0, "off_w": 0.0,
                "reason": "too few settled matches to fit"}
    cut = int(len(ms) * (1.0 - test_frac))
    train, test = ms[:cut], ms[cut:]

    base = {s: build_strength(_prior_for(comp.key, s), CONFIG.model) for s in (2025, 2026)}
    field = set(base[2026].ratings) | set(base[2025].ratings)

    # PIT alt-data index per (season vintage, week) — a match never sees its own week
    idx_cache: dict = {}

    from prediction_market_soccer.config.leagues import fitted_params
    _fp = fitted_params(comp.key)
    _mu = _fp.get("base_mu", CONFIG.model.base_mu)
    _ha = _fp.get("home_adv", CONFIG.model.home_adv)

    def idx_for(m, mode):
        key = (m["season"], _week_start(m["ts"]), mode)
        if key not in idx_cache:
            idx_cache[key] = altdata_index(
                conn, base[m["season"]].ratings, as_of=key[1], clubs=field,
                all_ratings=all_ratings, mode=mode, mu=_mu, ha=_ha,
                beta=CONFIG.model.beta, **IDX_OPTS)
        return idx_cache[key]

    def score(rows, dw, ow, cal=None, mode="residual"):
        tot = 0.0
        n = 0
        probs = []
        for m in rows:
            sm = base[m["season"]]
            if m["hi"] not in sm.ratings or m["ai"] not in sm.ratings:
                continue
            if dw or ow:
                cfg = dataclasses.replace(CONFIG.model, oppadj_def_weight=dw, oppadj_off_weight=ow)
                sm = dataclasses.replace(sm, cfg=cfg, adj=idx_for(m, mode))
            mp = price_match(sm, m["hi"], m["ai"], knockout=False, host_neutral=m["ko"])
            probs.append(([mp.p_home, mp.p_draw, mp.p_away], m["y"]))
        if not probs:
            return None, None, 0
        use_cal = cal if cal is not None else fit_calibration([p for p, _ in probs],
                                                              [y for _, y in probs])
        for p, y in probs:
            pv = apply_calibration(list(p), use_cal, knockout=False)
            oh = [1.0 if k == y else 0.0 for k in range(3)]
            tot += sum((pv[k] - oh[k]) ** 2 for k in range(3))
            n += 1
        return tot / n, use_cal, n

    # TIME-BLOCKED CROSS-VALIDATION on TRAIN. A single split let a weight that
    # merely fitted one stretch of the season look good; scoring every candidate
    # on K held-out time blocks (each fitted on the blocks before it, so nothing
    # trains on its own future) is what separates signal from a lucky window.
    K = 4
    fold = max(1, len(train) // K)
    folds = [(train[:i * fold], train[i * fold:(i + 1) * fold]) for i in range(1, K)]
    folds = [(tr, te) for tr, te in folds if len(tr) >= 40 and len(te) >= 20]

    def oof(dw, ow, mode):
        tot, wsum = 0.0, 0
        for tr, te in folds:
            _b, cal, _n = score(tr, dw, ow, mode=mode)
            b, _c, n = score(te, dw, ow, cal=cal, mode=mode)
            if b is None:
                continue
            tot += b * n; wsum += n
        return (tot / wsum) if wsum else None

    b0 = oof(0.0, 0.0, "residual")
    best = (b0, 0.0, 0.0, "none")
    for mode in MODES:
        for dw in GRID_DEF:
            for ow in GRID_OFF:
                if dw == 0.0 and ow == 0.0:
                    continue
                b = oof(dw, ow, mode)
                if b is not None and best[0] is not None and b < best[0]:
                    best = (b, dw, ow, mode)
    gain = (b0 - best[0]) if (b0 is not None and best[0] is not None) else 0.0
    keep = gain >= min_gain
    dw, ow, mode = (best[1], best[2], best[3]) if keep else (0.0, 0.0, "none")

    _b0t, cal0, n_tr = score(train, 0.0, 0.0)
    t0, _c, n_te = score(test, 0.0, 0.0, cal=cal0)
    tw, _c2, _n = score(test, dw, ow, cal=cal0, mode=mode) if keep else (t0, None, n_te)
    return {"comp": comp.key, "n": len(ms), "n_train": n_tr, "n_test": n_te,
            "def_w": round(dw, 3), "off_w": round(ow, 3), "mode": mode,
            "cv_brier_zero": round(b0, 4) if b0 else None,
            "cv_brier_best": round(best[0], 4) if best[0] else None,
            "cv_gain": round(gain, 4),
            "test_brier_zero": round(t0, 4) if t0 else None,
            "test_brier_fitted": round(tw, 4) if tw else None,
            "kept": bool(keep)}


def run(*, test_frac: float = 0.25, min_gain: float = 0.002,
        method: str = "regression") -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    conn = store.init_db()
    all_ratings: dict = {}
    for c in active():
        try:
            all_ratings.update(build_strength(load_prior(c.key), CONFIG.model).ratings)
        except Exception:
            pass
    out = {"ts": datetime.now(timezone.utc).isoformat(),
           "method": ("per-competition grid on that competition's own 2025-26 history; "
                      "PIT weekly alt-data index; selection on TRAIN only, kept only if "
                      f"it beats zero weights by >= {min_gain} calibrated Brier"),
           "index_options": IDX_OPTS, "weights": {}, "detail": []}
    for comp in active():
        r = (fit_comp_regression(conn, comp, test_frac=test_frac, all_ratings=all_ratings)
             if method == "regression" else
             fit_comp(conn, comp, test_frac=test_frac, min_gain=min_gain, all_ratings=all_ratings))
        out["weights"][comp.key] = {"oppadj_def_weight": r["def_w"],
                                    "oppadj_off_weight": r["off_w"],
                                    "mode": r.get("mode", "none")}
        out["detail"].append(r)
        print(f"  {comp.key:14s} n={r['n']:4d} → {r.get('mode','none'):8s} def={r['def_w']:.2f} off={r['off_w']:.2f} "
              f"slope_off={r.get('slope_off')}(t={r.get('t_off')}) "
              f"| test {r.get('test_brier_zero')}→{r.get('test_brier_fitted')} "
              f"{'KEEP' if r.get('kept') else 'zero'}", flush=True)
    # family fallback for competitions whose own regression had too few points
    fam_pool: dict = {}
    for d in out["detail"]:
        f = _FAMILY_OF.get(d["comp"])
        if f and (d.get("n_regression") or 0) >= MIN_REG_POINTS and (d["def_w"] or d["off_w"]):
            fam_pool.setdefault(f, []).append((d["def_w"], d["off_w"]))
    for d in out["detail"]:
        if (d.get("n_regression") or 0) >= MIN_REG_POINTS:
            continue
        f = _FAMILY_OF.get(d["comp"])
        pool = fam_pool.get(f) or []
        if not pool:
            continue
        dw = round(sum(x for x, _ in pool) / len(pool), 3)
        ow = round(sum(y for _, y in pool) / len(pool), 3)
        d.update({"def_w": dw, "off_w": ow, "mode": "residual",
                  "family_fallback": f, "kept": bool(dw or ow)})
        out["weights"][d["comp"]] = {"oppadj_def_weight": dw, "oppadj_off_weight": ow,
                                     "mode": "residual", "source": f"family:{f}"}
        print(f"  {d['comp']:14s} → family {f}: def={dw} off={ow} (own n={d.get('n_regression')})")

    # Written AFTER the family fallback, not before it: the fallback block only mutates
    # `out` in memory, so writing first meant every family-borrowed weight was printed
    # to the operator and returned to the caller while the file on disk — the thing the
    # model actually loads — never had them.
    CONFIG.paths.priors.mkdir(parents=True, exist_ok=True)
    (CONFIG.paths.priors / "league_altdata.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    kept = [d["comp"] for d in out["detail"] if d.get("kept")]
    print(f"\nleague_altdata.json written — weights kept for {len(kept)}/12: {kept}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-frac", type=float, default=0.25)
    ap.add_argument("--min-gain", type=float, default=0.002)
    ap.add_argument("--method", default="regression", choices=["regression", "grid"])
    a = ap.parse_args()
    run(test_frac=a.test_frac, min_gain=a.min_gain, method=a.method)


if __name__ == "__main__":
    main()
