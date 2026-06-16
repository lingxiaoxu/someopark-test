"""Full model run orchestrator (plan 02 §3.3 step 3, 05 §4).

Ties the modeling engine together and emits a single frontend-ready JSON:

    prior -> strength (calibrated) -> tournament MC -> golden boot (nested)
          -> group-match pricing -> data/output/model_run_<ts>.json (+ latest.json)

Run (inside someopark_run, with prediction_market/.env loaded for later live data):
    conda run -n someopark_run python -m prediction_market.model.run_model
    conda run -n someopark_run python -m prediction_market.model.run_model --full
    conda run -n someopark_run python -m prediction_market.model.run_model --emit-frontend

``--emit-frontend`` additionally writes worldcup_model.json into the someo-park
frontend's public/data dir (the only file this project writes there; it never
touches existing files). Off by default so the run is side-effect-free until you
opt in to wiring the UI.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import PriorSnapshot, load_prior
from prediction_market.model.golden_boot import (
    games_played_by_team,
    load_players,
    simulate_golden_boot,
)
from prediction_market.model.match_pricing import price_group_stage
from prediction_market.model.strength import build_strength
from prediction_market.model.tournament import simulate

CODE_VERSION_FALLBACK = "wc-model-v1"

# Honest disclosure shipped inside every run so the UI/consumer knows the v1
# simplifications (plan discipline: a model is an entry ticket, not edge).
MODEL_NOTES = [
    "v1: ratings reverse-fit to external prior expected points (plan 10 §5.1).",
    "Group tie-breaks: points>GD>GF>random (no head-to-head / full official chain).",
    "Knockout bracket: fixed balanced mapping, NOT the official 2026 third-place table.",
    "Golden-boot: real topscorers (shrunk rate + goals head-start) merged with seed favourites; "
    "mu lightly double-counts played games (v2, refine when sim conditions on results).",
    "No ensemble dispersion yet; sigma is a placeholder constant.",
    "NOT a tradable signal on its own — must be de-vigged vs live venue prices (plan 04).",
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


def build_payload(prior: PriorSnapshot, n_sims: int, seed: int, *,
                  ensemble: bool = False, update_results: bool = False, club_blend: bool = False) -> dict:
    name_of = {t.team_id: t.name for t in prior.teams}
    zh_of = {t.team_id: t.zh for t in prior.teams}
    group_of = {t.team_id: t.group for t in prior.teams}
    prior_adv = {t.team_id: t.p_advance for t in prior.teams}
    fifa_of = {t.team_id: t.fifa_rank for t in prior.teams}

    sm = build_strength(prior)
    if update_results:
        # Bayesian nudge from played results (plan 03 §1b/§7), needs ingested fixtures.
        from prediction_market.model.strength import update_strength_from_store
        sm = update_strength_from_store(sm)
    if club_blend:
        # Squad club/tournament form blend (plan 03 §1c).
        from prediction_market.model.club_aggregation import blend_into_strength, squad_attack_quality
        sm = blend_into_strength(sm, squad_attack_quality(season=CONFIG.soccer.season))
    tour = simulate(prior, sm, n_sims=n_sims, seed=seed)
    from prediction_market.ingest import store
    from prediction_market.model.tournament import eliminated_teams
    _gb_conn = store.init_db()
    _gb_players = load_players()
    _gb_team_of = {p.player_id: p.team_id for p in _gb_players}
    # Eliminated teams: their players play no more matches (golden boot frozen at
    # goals-so-far) AND their champion odds are 0 (overlay below). Same set for both.
    _elim = eliminated_teams(_gb_conn, all_team_ids=list(name_of.keys()))
    gb = simulate_golden_boot(
        tour, _gb_players, seed=seed + 1,
        games_played=games_played_by_team(_gb_conn),
        eliminated=_elim,
    )
    matches = price_group_stage(sm, prior)

    # Optional ensemble pass → real per-market dispersion (plan 10 §5.3),
    # replacing the placeholder sigma so sizing can discount disagreement.
    champ_sigma: dict[str, float] = {}
    ens_meta = None
    if ensemble:
        from prediction_market.model.ensemble import run_ensemble
        ens = run_ensemble(prior, n_variants=12, n_sims=max(15_000, n_sims // 4), seed=seed)
        champ_sigma = ens.p_champion_sigma
        ens_meta = {"n_variants": ens.n_variants, "n_sims": ens.n_sims}

    champion = [
        {
            "team_id": tid, "name": name_of[tid], "zh": zh_of[tid], "group": group_of[tid],
            "fifa_rank": fifa_of.get(tid),
            "p_champion": round(tour.p_champion[tid], 5),
            "p_champion_sigma": round(champ_sigma[tid], 5) if tid in champ_sigma else None,
            "p_final": round(tour.p_final[tid], 5),
            "p_sf": round(tour.p_sf[tid], 5),
            "p_qf": round(tour.p_qf[tid], 5),
            "p_r16": round(tour.p_r16[tid], 5),
            "p_advance_model": round(tour.p_advance[tid], 5),
            "p_advance_prior": prior_adv[tid],
            "e_matches": round(tour.e_matches[tid], 3),
            "rating": round(sm.ratings[tid], 4),
        }
        for tid in tour.team_ids
    ]

    # Elimination overlay: once the knockouts begin, a team that is OUT (lost a KO
    # tie, or never qualified from its group) can no longer win — force its champion
    # and stage probabilities to 0 and renormalise the title odds across survivors.
    # No-op during the group stage (eliminated set is empty).
    elim = _elim
    if elim:
        for r in champion:
            if r["team_id"] in elim:
                for k in ("p_champion", "p_final", "p_sf", "p_qf", "p_r16", "p_advance_model", "e_matches"):
                    r[k] = 0.0
                r["eliminated"] = True
        surviving_mass = sum(r["p_champion"] for r in champion)
        if surviving_mass > 0:
            for r in champion:
                r["p_champion"] = round(r["p_champion"] / surviving_mass, 5)
    champion.sort(key=lambda r: -r["p_champion"])

    _gb_goals_of = {p.player_id: p.goals_so_far for p in _gb_players}
    golden_boot = [
        {
            "player_id": pid, "name": gb.player_names[pid],
            "team_id": _gb_team_of.get(pid),
            "team": name_of.get(_gb_team_of.get(pid), ""),
            "zh": zh_of.get(_gb_team_of.get(pid), ""),
            "goals": int(_gb_goals_of.get(pid, 0)),     # current WC goals scored so far
            "p_golden_boot": round(gb.p_golden_boot[pid], 5),
            "e_goals": round(gb.e_goals[pid], 3),
        }
        for pid in gb.player_ids
    ]
    golden_boot.sort(key=lambda r: -r["p_golden_boot"])

    group_matches = [
        {
            "home_id": mp.home_id, "home": name_of[mp.home_id],
            "away_id": mp.away_id, "away": name_of[mp.away_id],
            "group": group_of[mp.home_id],
            "p_home": round(mp.p_home, 4), "p_draw": round(mp.p_draw, 4), "p_away": round(mp.p_away, 4),
            "p_over_2_5": round(mp.p_over_2_5, 4), "p_btts": round(mp.p_btts, 4),
        }
        for mp in matches
    ]

    return {
        "meta": {
            "run_ts": datetime.now(timezone.utc).isoformat(),
            "code_version": _code_version(),
            "n_sims": n_sims,
            "prior_id": prior.prior_id,
            "prior_as_of": prior.as_of,
            "ensemble": ens_meta,
            "model_notes": MODEL_NOTES,
        },
        "champion": champion,
        "golden_boot": golden_boot,
        "group_matches": group_matches,
    }


def refresh_champion(*, n_sims: int | None = None, seed: int | None = None) -> dict:
    """Re-simulate the tournament with the LATEST results (strength nudged by finished
    matches + eliminated teams forced to 0% in the knockouts) and publish worldcup_model.json
    to the output + frontend dirs. Called after each match so the champion/golden-boot odds
    refresh as the tournament unfolds. Returns the payload.
    """
    prior = load_prior()
    n_sims = n_sims or CONFIG.model.n_sims_quicklook
    seed = seed if seed is not None else CONFIG.model.random_seed
    payload = build_payload(prior, n_sims=n_sims, seed=seed, update_results=True)
    write_outputs(payload, emit_frontend=True)
    return payload


def write_outputs(payload: dict, *, emit_frontend: bool) -> list[Path]:
    CONFIG.paths.ensure()
    ts = payload["meta"]["run_ts"].replace(":", "").replace("-", "").split(".")[0]
    written: list[Path] = []

    run_file = CONFIG.paths.output / f"model_run_{ts}.json"
    latest = CONFIG.paths.output / "latest.json"
    for p in (run_file, latest):
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(p)

    if emit_frontend:
        fe_dir = CONFIG.paths.frontend_data
        if fe_dir.exists():
            fe_file = fe_dir / "worldcup_model.json"
            fe_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            written.append(fe_file)
        else:
            print(f"[warn] frontend data dir not found, skipped: {fe_dir}")

    # Persist the run to the store (plan 05 §2) — best-effort, never block output.
    try:
        from prediction_market.ingest import store
        conn = store.init_db()
        store.persist_model_run(
            conn, payload["meta"]["run_ts"], n_sims=payload["meta"]["n_sims"],
            code_version=payload["meta"]["code_version"],
            champion=payload["champion"], golden_boot=payload["golden_boot"],
        )
    except Exception as e:
        print(f"[warn] store persistence skipped: {e}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="World Cup 2026 model run")
    ap.add_argument("--full", action="store_true", help="use full N (config.n_sims_tournament)")
    ap.add_argument("--n-sims", type=int, default=None, help="override simulation count")
    ap.add_argument("--seed", type=int, default=CONFIG.model.random_seed)
    ap.add_argument("--ensemble", action="store_true",
                    help="run the variant ensemble to attach champion-prob dispersion (slower)")
    ap.add_argument("--update-results", action="store_true",
                    help="Bayesian-update team strength from played results in the store")
    ap.add_argument("--club-blend", action="store_true",
                    help="blend squad club/tournament form into team strength (plan 03 §1c)")
    ap.add_argument("--emit-frontend", action="store_true",
                    help="also write worldcup_model.json to the frontend public/data dir")
    args = ap.parse_args()

    n_sims = args.n_sims or (CONFIG.model.n_sims_tournament if args.full else CONFIG.model.n_sims_quicklook)
    prior = load_prior()
    payload = build_payload(prior, n_sims=n_sims, seed=args.seed,
                            ensemble=args.ensemble, update_results=args.update_results, club_blend=args.club_blend)
    written = write_outputs(payload, emit_frontend=args.emit_frontend)

    top = payload["champion"][:5]
    print(f"model run OK (N={n_sims}, version={payload['meta']['code_version']})")
    print("top-5 champion:", ", ".join(f"{r['name']} {r['p_champion']:.1%}" for r in top))
    print("top-3 golden boot:",
          ", ".join(f"{r['name']} {r['p_golden_boot']:.1%}" for r in payload["golden_boot"][:3]))
    for p in written:
        print(f"  wrote {p}")


if __name__ == "__main__":
    main()
