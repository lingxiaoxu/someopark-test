"""Full model run orchestrator — club edition (TRANSFORM_PLAN §2.2 run_model row).

Per enabled competition: club prior → live strength (blends) → season Monte-Carlo
(league_season) → upcoming-fixture pricing → one frontend-ready payload:

    data/output/soccer_model.json  (+ model_run_<ts>.json archive, latest.json)
    → public/data/soccer/soccer_model.json  (--emit-frontend)

Payload shape (the frontend league→matches hierarchy, §3.7):
    {meta, leagues: [{league, name, zh, kind, n_remaining, table, season_odds:[...],
                      matches:[...]}]}

The WC elimination/confirmed-reach overlays are replaced by the mathematical-lock
snap inside league_season (a clinched title reads 1.0 because every simulated
path says so). Champion ¢ columns are attached in Phase 3 (venue discovery).

Run:
    conda run -n someopark_run python -m prediction_market_soccer.model.run_model --emit-frontend
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active, get
from prediction_market_soccer.ingest.club_prior import load_prior
from prediction_market_soccer.model.league_season import simulate_season
from prediction_market_soccer.model.match_pricing import price_upcoming_fixtures
from prediction_market_soccer.model.squad_strength import build_strength_live
from prediction_market_soccer.model.top_scorer import top_scorer_board

CODE_VERSION_FALLBACK = "soccer-model-v1"

MODEL_NOTES = [
    "v1: ratings reverse-fit to club-prior anchor ppr (last-season table + ClubElo when reachable;",
    "market anchor lands with venue discovery). Per-league base_mu/home_adv fitted from",
    "season-1 results (ops/fit_league_params).",
    "Season sim tie-breaks: pts > GD > GF for every league; La Liga/Serie A H2H approximated",
    "by GD (R4, exact H2H in a later phase). Bundesliga/Ligue1 relegation playoff spot",
    "counted at half weight.",
    "Two-legged ties priced with the leg-1 aggregate carried in (no away-goals rule);",
    "CONMEBOL level aggregates go straight to pens, UEFA play ET first.",
    "NOT a tradable signal on its own — must be de-vigged vs live venue prices.",
]


def _code_version() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=CONFIG.paths.root, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return f"{CODE_VERSION_FALLBACK}@{out.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return CODE_VERSION_FALLBACK


def build_payload(*, n_sims: int = 200_000, seed: int | None = None,
                  champ_cents: dict | None = None, conn=None) -> dict:
    from prediction_market_soccer.ingest import store
    conn = conn or store.init_db()
    seed = seed if seed is not None else CONFIG.model.random_seed
    champ_cents = champ_cents or {}

    leagues_out = []
    for comp in active():
        try:
            prior = load_prior(comp.key)
        except Exception as e:  # noqa: BLE001 — a missing prior must not kill the run
            print(f"[run_model:{comp.key}] prior unavailable ({e}) — skipped")
            continue
        name_of = {t.club_id: t.name for t in prior.teams}
        zh_of = {t.club_id: (t.zh or "") for t in prior.teams}
        elo_rank = {t.club_id: t.elo_rank for t in prior.teams}
        logo = {}
        for r in conn.execute("SELECT club_id, logo FROM club_registry WHERE comp=?", (comp.key,)):
            logo[r["club_id"]] = r["logo"]

        sm = build_strength_live(conn, prior, league=comp.key, xg_form=True)
        try:
            from prediction_market_soccer.model.strength_cache import save_model
            save_model(sm, comp.key, conn)   # live exports load this instead of refitting
        except Exception as e:  # noqa: BLE001
            print(f"[run_model:{comp.key}] ratings cache save failed: {e}")
        sim = simulate_season(conn, comp.key, sm, n_sims=n_sims, seed=seed)

        # Reset per competition — the KO branch fills this, the league branch does not,
        # and a value left over from the previous comp would publish one cup's ladder
        # against another competition's clubs.
        ladder: dict[str, dict] = {}
        # KO/pre-draw guard: a CUP is never crowned by a table. `cup_two_leg`
        # (Libertadores/Sudamericana) plays a group phase whose standings ARE
        # populated, so the "has a live table" test used to pass and the group
        # leader was published as champion at ~100% — even after being knocked
        # out (Botafogo 1.0 while eliminated in the R16). A cup's champion odds
        # come from the KO-tree sim (ucl_phase) whenever a KO bracket exists;
        # a swiss/league comp still falls back to the table sim.
        table_alive = sim.n_remaining > 0 or any(
            (t.get("played") or 0) > 0 for t in sim.table_now)
        # A SWISS competition (UCL/UEL/UECL) is a league phase followed by a
        # knockout, so which engine is right depends on where the season stands —
        # it must not be pinned to either. Before the draw there is no field and
        # no fixtures: nothing to simulate. Once the draw lands, the 36-club
        # league phase IS a table and league_season already prices its rank cuts
        # (p_qual_direct / p_qual_playoff). Only when that phase is over and a
        # bracket exists does the KO tree take over. The test is the data itself
        # — league-phase fixtures on the calendar — not the date.
        swiss_league_phase = (
            comp.kind == "swiss_ucl"
            and sim.n_remaining > 0
            and len(sim.club_ids) <= comp.n_teams * 1.2)   # the drawn field, not the qualifying superset
        season_valid = (
            table_alive and comp.kind != "cup_two_leg"
            and (comp.kind != "swiss_ucl" or swiss_league_phase))
        if not season_valid:
            try:
                from prediction_market_soccer.model.ucl_phase import ko_ladder
                _lad = ko_ladder(conn, comp.key, sm) or {}
            except Exception as e:  # noqa: BLE001
                print(f"[run_model:{comp.key}] KO champion sim unavailable ({e})")
                _lad = {}
            ko = _lad.get("champion")
            # Reach-round rungs (RO16/RO8/RO4/FINALIST) — Kalshi lists each as its own
            # season market and the registry has always carried the tickers; the board
            # could not price them because nothing produced these probabilities.
            ladder = {f: _lad.get(f) or {} for f in ("ro16", "ro8", "ro4", "finalist")}
            sim.p_champion = (ko or {})
            sim.p_top_n = {}; sim.p_relegation = {}; sim.p_last = {}
            sim.p_qual_direct = sim.p_qual_playoff = None
            sim.e_points = {}; sim.e_rank = {}
            # The KO tree runs on the BRACKET, the club list came from the standings
            # table, and in a cup those two sets differ — a club that entered at the
            # round of 32 never appears in a group table. Iterating club_ids alone
            # dropped those rows, so Sudamericana published champion odds summing to
            # 0.57 while the simulation itself summed to 1.0. Union the two.
            if ko:
                sim.club_ids = sorted(set(sim.club_ids) | set(ko))

        cents = champ_cents.get(comp.key) or {}
        try:
            from prediction_market_soccer.venues.champion_prices import (
                _norm_person, topscorer_cents)
            _ts_cents = topscorer_cents(comp.key)
        except Exception as e:  # noqa: BLE001 — a missing market must not sink the board
            print(f"[run_model:{comp.key}] top-scorer market skipped ({type(e).__name__}: {e})")
            from prediction_market_soccer.venues.champion_prices import _norm_person
            _ts_cents = {}
        # Honest empty state: when NO champion distribution could be computed
        # (pre-draw swiss, or a cup with no live KO bracket yet) the season-odds
        # probabilities are UNKNOWN, not 0% — emit null so the frontend shows
        # "—/待抽签" instead of a confident zero for all 153 clubs.
        odds_state = ("ok" if sim.p_champion else
                      ("pending_draw" if comp.kind == "swiss_ucl" else "pending_bracket"))
        _unknown = not sim.p_champion

        def _p(d, cid):
            return None if _unknown else (d.get(cid, 0.0) if d else 0.0)

        season_odds = []
        for cid in sim.club_ids:
            season_odds.append({
                "club_id": cid, "name": name_of.get(cid, cid), "zh": zh_of.get(cid, ""),
                "logo": logo.get(cid),
                "elo_rank": elo_rank.get(cid),
                "p_champion": _p(sim.p_champion, cid),
                "p_top_n": _p(sim.p_top_n, cid),
                "p_relegation": _p(sim.p_relegation, cid),
                "p_last": _p(sim.p_last, cid),
                "p_qual_direct": (sim.p_qual_direct or {}).get(cid),
                "p_qual_playoff": (sim.p_qual_playoff or {}).get(cid),
                **{f"p_{f}": (ladder.get(f) or {}).get(cid) for f in
                   ("ro16", "ro8", "ro4", "finalist")},
                "e_points": sim.e_points.get(cid),
                "e_rank": sim.e_rank.get(cid),
                "rating": round(sm.ratings.get(cid, 0.0), 4),
                "kalshi_champ_c": (cents.get(cid) or {}).get("kalshi_c"),
                "poly_champ_c": (cents.get(cid) or {}).get("poly_c"),
            })
        season_odds.sort(key=lambda r: (-(r["p_champion"] or 0),
                                        (r["elo_rank"] if r["elo_rank"] is not None else 9999)))

        matches = []
        for mp in price_upcoming_fixtures(sm, conn, comp.key, days=8):
            matches.append({
                "home_id": mp.home_id, "home": name_of.get(mp.home_id, mp.home_id),
                "away_id": mp.away_id, "away": name_of.get(mp.away_id, mp.away_id),
                "p_home": round(mp.p_home, 4), "p_draw": round(mp.p_draw, 4),
                "p_away": round(mp.p_away, 4),
                "p_over_2_5": round(mp.p_over_2_5, 4), "p_btts": round(mp.p_btts, 4),
                "knockout": mp.knockout,
                "p_home_advance": round(mp.p_home_advance, 4) if mp.p_home_advance is not None else None,
            })

        table = [{**row, "name": name_of.get(row["club_id"], row["club_id"]),
                  "zh": zh_of.get(row["club_id"], "")} for row in sim.table_now]

        leagues_out.append({
            "league": comp.key, "name": comp.name, "zh": comp.zh, "kind": comp.kind,
            "n_teams": len(sim.club_ids), "n_remaining": sim.n_remaining,
            "top_n": comp.top_n, "releg_direct": comp.releg_direct,
            "releg_playoff": comp.releg_playoff,
            "table": table, "season_odds": season_odds, "matches": matches,
            # per-competition top-scorer race (model/top_scorer) — the club
            # counterpart of the WC golden boot; [] for cup competitions, whose
            # "matches remaining" depends on surviving a bracket.
            # the board carries club_id; attach the display name/zh here where the
            # per-competition name map already exists (the frontend resolves its own
            # translation from club_id, and falls back to these).
            "top_scorer": [
                {**r, "club": {"id": r["club_id"],
                               "name": name_of.get(r["club_id"], r["club_id"]),
                               "zh": zh_of.get(r["club_id"], "")},
                 # Market column. Empty until Kalshi opens the season top-scorer
                 # series — measured 2026-08-27, none of the seven candidate series
                 # had a single open event this early in the season — so a null here
                 # means "not listed yet", not "no edge".
                 "kalshi_c": _ts_cents.get(_norm_person(r["name"])),
                 "edge_vs_kalshi": (
                     round(r["p_top_scorer"] * 100 - _ts_cents[_norm_person(r["name"])], 1)
                     if _norm_person(r["name"]) in _ts_cents else None)}
                for r in top_scorer_board(conn, comp.key, n_sims=50_000, seed=seed)],
            "odds_state": odds_state,
            # zoned competitions (Argentina Apertura/Clausura = two 15-club zones,
            # top 8 of EACH advance): the frontend groups the table by this and the
            # league-wide p_top_n/e_rank are NOT meaningful across zones.
            "zones": sorted({(r.get("zone") or "") for r in table} - {""}) or None,
        })
        top = season_odds[0] if season_odds else {}
        _tp = top.get("p_champion")
        print(f"[run_model:{comp.key}] N={n_sims} remaining={sim.n_remaining} "
              f"top={top.get('club_id')} "
              + (f"{_tp:.1%}" if _tp is not None else f"— ({odds_state})"))

    return {
        "meta": {
            "run_ts": datetime.now(timezone.utc).isoformat(),
            "code_version": _code_version(),
            "n_sims": n_sims,
            "model_notes": MODEL_NOTES,
        },
        "leagues": leagues_out,
    }


def write_outputs(payload: dict, *, emit_frontend: bool) -> list[Path]:
    CONFIG.paths.ensure()
    ts = payload["meta"]["run_ts"].replace(":", "").replace("-", "").split(".")[0]
    written: list[Path] = []

    run_file = CONFIG.paths.output / f"model_run_{ts}.json"
    latest = CONFIG.paths.output / "latest.json"
    model_file = CONFIG.paths.output / "soccer_model.json"
    txt = json.dumps(payload, ensure_ascii=False, indent=1)
    for p in (run_file, latest, model_file):
        p.write_text(txt, encoding="utf-8")
        written.append(p)

    runs = sorted(CONFIG.paths.output.glob("model_run_*.json"))
    for old in runs[:-10]:
        try:
            old.unlink()
        except OSError:
            pass

    if emit_frontend:
        fe_file = CONFIG.paths.frontend_data / "soccer_model.json"
        fe_file.write_text(txt, encoding="utf-8")
        written.append(fe_file)
    return written


def refresh_model(*, n_sims: int | None = None, seed: int | None = None) -> dict:
    """Re-simulate every competition with the LATEST results and publish
    soccer_model.json to output + frontend. The soccer analogue of the WC
    ``refresh_champion`` (same call sites in refresh_all/live_refresh)."""
    try:
        from prediction_market_soccer.venues.champion_prices import champion_cents_all
        cc = champion_cents_all()
    except Exception as e:  # noqa: BLE001 — venues down must not block the model
        print(f"[refresh_model] champion ¢ skipped: {e}")
        cc = {}
    payload = build_payload(n_sims=n_sims or 200_000, seed=seed, champ_cents=cc)
    write_outputs(payload, emit_frontend=True)
    return payload


# Back-compat alias (copied callers in refresh_all/live_refresh say refresh_champion).
refresh_champion = refresh_model


def main() -> None:
    ap = argparse.ArgumentParser(description="Club soccer model run (12-comp registry)")
    ap.add_argument("--n-sims", type=int, default=None)
    ap.add_argument("--full", action="store_true", help="published run: 500k sims")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--emit-frontend", action="store_true")
    args = ap.parse_args()
    n = args.n_sims or (500_000 if args.full else 100_000)
    payload = build_payload(n_sims=n, seed=args.seed)
    written = write_outputs(payload, emit_frontend=args.emit_frontend)
    print(f"model run OK ({len(payload['leagues'])} leagues, N={n})")
    for p in written:
        print(f"  wrote {p}")


if __name__ == "__main__":
    main()
