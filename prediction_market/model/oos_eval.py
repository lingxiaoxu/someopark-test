"""Out-of-sample evaluation (plan 03 §7, 06 §6) — the pre-real-money gate.

The pre-match model was frozen before the tournament; every already-played match
is therefore genuine out-of-sample data. We score the frozen model's W/D/L
predictions against realised results (Brier / Log-loss, with bootstrap CIs since
the sample is tiny) and look for SYSTEMATIC bias (over/under-estimating draws,
favourite mis-calibration, goal-total bias) — plan 03 §7.

DISCIPLINE (plan 03 §7): with ~a dozen matches this is a DIRECTIONAL health
check that may trigger structural fixes only — never fine-tune parameters to it
(overfitting). Reads realised results straight from the local store (real
API-Football data), so it runs with zero extra API calls.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.ingest import store
from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.calibrate import bootstrap_ci, brier_score, log_loss, reliability_curve
from prediction_market.model.match_pricing import price_match
from prediction_market.model.strength import StrengthModel, build_strength

_FINISHED = ("FT", "AET", "PEN")


@dataclass
class OOSReport:
    n_matches: int
    brier: float
    brier_ci95: tuple[float, float]
    log_loss: float
    # Reference baselines for context.
    brier_uniform: float            # always predicting 1/3,1/3,1/3
    # Systematic-bias diagnostics.
    pred_draw_rate: float
    obs_draw_rate: float
    pred_home_rate: float
    obs_home_rate: float
    favourite_hit_rate: float       # how often the model's pick won
    pred_avg_total_goals: float
    obs_avg_total_goals: float
    notes: list[str]


def _canonical_map(conn) -> dict[int, str]:
    rows = conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL"
    ).fetchall()
    return {r["api_id"]: r["canonical_team_id"] for r in rows}


def evaluate(conn=None, sm: StrengthModel | None = None) -> OOSReport:
    conn = conn or store.init_db()
    sm = sm or build_strength(load_prior())
    cmap = _canonical_map(conn)

    rows = conn.execute(
        "SELECT home_api_id, away_api_id, home_goals, away_goals FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL".format(
            ",".join("?" * len(_FINISHED))), _FINISHED).fetchall()

    probs, outcomes = [], []          # 3-way: 0=home,1=draw,2=away
    exp_totals, obs_totals = [], []
    draw_event_p, draw_event_occ = [], []
    fav_hits = 0
    skipped = 0
    for r in rows:
        hid, aid = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hid is None or aid is None or hid not in sm.ratings or aid not in sm.ratings:
            skipped += 1
            continue
        mp = price_match(sm, hid, aid, knockout=False)
        p = [mp.p_home, mp.p_draw, mp.p_away]
        hg, ag = r["home_goals"], r["away_goals"]
        outcome = 0 if hg > ag else (1 if hg == ag else 2)
        probs.append(p)
        outcomes.append(outcome)
        exp_totals.append((mp.lam_home + mp.lam_away))
        obs_totals.append(hg + ag)
        draw_event_p.append(mp.p_draw)
        draw_event_occ.append(1.0 if outcome == 1 else 0.0)
        if int(np.argmax(p)) == outcome:
            fav_hits += 1

    n = len(probs)
    notes: list[str] = []
    if skipped:
        notes.append(f"skipped {skipped} fixtures with unmapped teams")
    if n == 0:
        notes.append("no scored matches available yet — run soccer_ingest --scope results")
        return OOSReport(0, float("nan"), (float("nan"), float("nan")), float("nan"),
                         float("nan"), *(float("nan"),) * 6, notes=notes)

    probs_arr = np.array(probs)
    outcomes_arr = np.array(outcomes)
    per_sample_brier = np.sum((probs_arr - np.eye(3)[outcomes_arr]) ** 2, axis=1)

    pred_draw = float(np.mean(draw_event_p))
    obs_draw = float(np.mean(draw_event_occ))
    pred_home = float(np.mean(probs_arr[:, 0]))
    obs_home = float(np.mean(outcomes_arr == 0))
    if obs_draw - pred_draw > 0.08:
        notes.append(f"model may UNDER-estimate draws (pred {pred_draw:.2f} vs obs {obs_draw:.2f})")
    elif pred_draw - obs_draw > 0.08:
        notes.append(f"model may OVER-estimate draws (pred {pred_draw:.2f} vs obs {obs_draw:.2f})")
    gt_pred, gt_obs = float(np.mean(exp_totals)), float(np.mean(obs_totals))
    if abs(gt_pred - gt_obs) > 0.5:
        notes.append(f"goal-total bias: pred {gt_pred:.2f} vs obs {gt_obs:.2f}")
    notes.append("DIRECTIONAL check only — do NOT fine-tune params to this (plan 03 §7).")

    return OOSReport(
        n_matches=n,
        brier=brier_score(probs, outcomes),
        brier_ci95=bootstrap_ci(per_sample_brier),
        log_loss=log_loss(probs, outcomes),
        brier_uniform=brier_score([[1 / 3, 1 / 3, 1 / 3]] * n, outcomes),
        pred_draw_rate=pred_draw, obs_draw_rate=obs_draw,
        pred_home_rate=pred_home, obs_home_rate=obs_home,
        favourite_hit_rate=fav_hits / n,
        pred_avg_total_goals=gt_pred, obs_avg_total_goals=gt_obs,
        notes=notes,
    )


def main() -> None:
    rep = evaluate()
    CONFIG.paths.ensure()
    out = CONFIG.paths.output / "oos_report.json"
    out.write_text(json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OOS over {rep.n_matches} played matches")
    if rep.n_matches:
        print(f"  Brier      {rep.brier:.4f}  (95% CI {rep.brier_ci95[0]:.3f}-{rep.brier_ci95[1]:.3f}); "
              f"uniform baseline {rep.brier_uniform:.4f}")
        print(f"  Log-loss   {rep.log_loss:.4f}")
        print(f"  draws      pred {rep.pred_draw_rate:.2f} vs obs {rep.obs_draw_rate:.2f}")
        print(f"  fav hit    {rep.favourite_hit_rate:.2f}")
        print(f"  goals/game pred {rep.pred_avg_total_goals:.2f} vs obs {rep.obs_avg_total_goals:.2f}")
    for nnote in rep.notes:
        print(f"  • {nnote}")
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
