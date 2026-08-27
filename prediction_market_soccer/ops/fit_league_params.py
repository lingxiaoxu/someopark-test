"""Fit per-competition base_mu / home_adv from LAST-SEASON results (TRANSFORM_PLAN C2).

Model: lambda_home = exp(mu + ha + beta*d), lambda_away = exp(mu - beta*d).
Averaged over a season d ~ 0, so the season means identify the constants:

    mu = ln(mean away goals)          ha = ln(mean home goals / mean away goals)

Neutral-venue rounds (finals) and knockout rounds are excluded — the fit wants
the plain-league home-edge. UEFA comps have no season-1 "league" history in our
DB (different format each year); they inherit the mean of the five big leagues
(disclosed v1; per-comp calibration replaces it in Phase 6).

One /fixtures call per comp for season-1 (12 requests, one-off; cached in the
fixture table like everything else). Writes data/priors/league_params.json.

Run: conda run -n someopark_run python -m prediction_market_soccer.ops.fit_league_params
"""
from __future__ import annotations

import json
import math

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import Stage, active, stage_of
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.api_football import ApiFootball, BudgetExceededError
from prediction_market_soccer.ingest.soccer_ingest import _FINISHED, _fixture_row

_OUT = CONFIG.paths.priors / "league_params.json"


def _ingest_last_season(api, conn, comp) -> int:
    wm = f"fixtures_hist:{comp.key}:{comp.season - 1}"
    if store.is_fresh(conn, wm, 30 * 24 * 3600):
        return 0
    items = api.fixtures(league=comp.api_football_id, season=comp.season - 1)
    for it in items:
        store.upsert(conn, "fixture", _fixture_row(it), pk=["api_id"])
    store.set_watermark(conn, wm, note=f"{len(items)} fixtures")
    conn.commit()
    print(f"[hist:{comp.key}] ingested {len(items)} season-{comp.season-1} fixtures")
    return len(items)


def fit(conn=None, *, with_ingest: bool = True) -> dict:
    conn = conn or store.init_db()
    api = ApiFootball(conn) if with_ingest else None
    params: dict[str, dict] = {}
    league_fits = []
    for comp in active():
        if with_ingest:
            try:
                _ingest_last_season(api, conn, comp)
            except BudgetExceededError as e:
                print(f"[fit] budget: {e}")
                break
            except Exception as e:  # noqa: BLE001
                print(f"[fit:{comp.key}] history ingest failed ({e}) — skipped")
        rows = conn.execute(
            "SELECT round, home_goals, away_goals FROM fixture "
            "WHERE league_id=? AND season=? AND status_short IN ({}) "
            "AND home_goals IS NOT NULL".format(",".join("?" * len(_FINISHED))),
            (comp.api_football_id, comp.season - 1) + tuple(_FINISHED)).fetchall()
        gh = ga = n = 0
        for r in rows:
            if stage_of(comp.key, r["round"]) != Stage.LEAGUE:
                continue   # exclude KO/neutral rounds from the home-edge fit
            gh += r["home_goals"]; ga += r["away_goals"]; n += 1
        if n >= 100:       # need a real season of league rounds
            mh, ma = gh / n, ga / n
            params[comp.key] = {
                "base_mu": round(math.log(ma), 4),
                "home_adv": round(math.log(mh / ma), 4),
                "n_matches": n, "mean_home_goals": round(mh, 3), "mean_away_goals": round(ma, 3),
                "season": comp.season - 1,
            }
            league_fits.append((params[comp.key]["base_mu"], params[comp.key]["home_adv"]))
            print(f"[fit:{comp.key}] n={n}  GH={mh:.3f} GA={ma:.3f} → mu={params[comp.key]['base_mu']} "
                  f"ha={params[comp.key]['home_adv']}")
        else:
            params[comp.key] = {"n_matches": n, "note": "insufficient league history — inherits big-5 mean"}
            print(f"[fit:{comp.key}] only {n} league matches — will inherit big-5 mean")
    # comps without history inherit the mean of the fitted big leagues (v1, disclosed)
    if league_fits:
        mu_bar = sum(f[0] for f in league_fits) / len(league_fits)
        ha_bar = sum(f[1] for f in league_fits) / len(league_fits)
        for comp in active():
            if "base_mu" not in params.get(comp.key, {}):
                params[comp.key].update({"base_mu": round(mu_bar, 4), "home_adv": round(ha_bar, 4),
                                         "inherited": True})
    _OUT.write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[fit] wrote {_OUT.name}")
    return params


if __name__ == "__main__":
    fit()
