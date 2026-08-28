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

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ingest.club_prior import load_prior
from prediction_market_soccer.model.calibrate import bootstrap_ci, brier_score, log_loss, reliability_curve
from prediction_market_soccer.model.match_pricing import price_match
from prediction_market_soccer.model.strength import StrengthModel, build_strength

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
    # The baseline that actually has to be beaten. "Uniform" means 1/3-1/3-1/3, which
    # nobody would forecast — home teams win ~45% of club matches and everyone knows it.
    # Climatology (always predict the observed base rates) is the free forecast, so it is
    # the honest zero point for skill. Reported alongside a PAIRED t-stat, because the
    # bootstrap CI on `brier` answers a different question than "is the model better".
    brier_climatology: float = float("nan")
    skill_vs_climatology: float = float("nan")     # climatology − model (positive = better)
    skill_t: float = float("nan")                  # paired t on the per-match difference


def _canonical_map(conn) -> dict[int, str]:
    rows = conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL"
    ).fetchall()
    return {r["api_id"]: r["canonical_team_id"] for r in rows}


def evaluate(conn=None, sm: StrengthModel | None = None, *, since_days: float = 60.0) -> OOSReport:
    """Club edition: pooled across every enabled comp's RECENT settled fixtures,
    each priced with its own league model + stage-aware knockout flag (C1)."""
    from prediction_market_soccer.config.leagues import Stage, active, stage_of
    from prediction_market_soccer.model.pit_strength import WalkForwardStrength

    conn = conn or store.init_db()
    cmap = _canonical_map(conn)

    # Out-of-sample means the model did not see the result. A caller-supplied `sm` is
    # taken as-is (tests pass a fixed model); otherwise each match is priced by the
    # model frozen before its week — see model/pit_strength for the measured leak.
    wf = None if sm is not None else WalkForwardStrength(conn)
    work: list[tuple] = []   # (sm, knockout, row)
    for comp in active():
        for r in conn.execute(
            "SELECT round, home_api_id, away_api_id, home_goals, away_goals, kickoff_ts FROM fixture "
            "WHERE league_id=? AND season=? AND status_short IN ({}) AND home_goals IS NOT NULL "
            "AND kickoff_ts >= datetime('now', ?)".format(",".join("?" * len(_FINISHED))),
            (comp.api_football_id, comp.season, *_FINISHED, f"-{since_days} days")):
            csm = sm if sm is not None else wf.for_match(comp.key, r["kickoff_ts"])
            if csm is None:
                break
            ko = stage_of(comp.key, r["round"]) in (Stage.CUP_TWO_LEG, Stage.CUP_SINGLE)
            work.append((csm, ko, r))

    probs, outcomes = [], []          # 3-way: 0=home,1=draw,2=away
    exp_totals, obs_totals = [], []
    draw_event_p, draw_event_occ = [], []
    fav_hits = 0
    skipped = 0
    for csm, ko, r in work:
        hid, aid = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if hid is None or aid is None or hid not in csm.ratings or aid not in csm.ratings:
            skipped += 1
            continue
        mp = price_match(csm, hid, aid, knockout=ko)
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
        nan = float("nan")
        return OOSReport(n_matches=0, brier=nan, brier_ci95=(nan, nan), log_loss=nan,
                         brier_uniform=nan, pred_draw_rate=nan, obs_draw_rate=nan,
                         pred_home_rate=nan, obs_home_rate=nan, favourite_hit_rate=nan,
                         pred_avg_total_goals=nan, obs_avg_total_goals=nan, notes=notes)

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
    # Climatology = always forecast the OBSERVED base rates. This is generous to the
    # baseline (it is fitted in-sample on the very matches it scores), so a model that
    # cannot beat it here has not demonstrated skill.
    base = np.array([np.mean(outcomes_arr == k) for k in range(3)])
    per_sample_clim = np.sum((base[None, :] - np.eye(3)[outcomes_arr]) ** 2, axis=1)
    brier_clim = float(np.mean(per_sample_clim))
    diff = per_sample_clim - per_sample_brier          # positive = model better
    skill = float(np.mean(diff))
    se = float(np.std(diff, ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    t_stat = skill / se if se and np.isfinite(se) and se > 0 else float("nan")
    if np.isfinite(t_stat) and t_stat < 2.0:
        notes.append(f"skill over base rates NOT significant: {skill:+.4f} Brier, "
                     f"t={t_stat:.2f} on n={n}")
    notes.append("DIRECTIONAL check only — do NOT fine-tune params to this (plan 03 §7).")

    return OOSReport(
        brier_climatology=round(brier_clim, 4),
        skill_vs_climatology=round(skill, 4),
        skill_t=round(t_stat, 2) if np.isfinite(t_stat) else float("nan"),
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
        print(f"  Brier      {rep.brier:.4f}  (95% CI {rep.brier_ci95[0]:.3f}-{rep.brier_ci95[1]:.3f})")
        print(f"  baselines  uniform {rep.brier_uniform:.4f} · base-rates {rep.brier_climatology:.4f} "
              f"→ skill {rep.skill_vs_climatology:+.4f} (paired t={rep.skill_t:.2f})")
        print(f"  Log-loss   {rep.log_loss:.4f}")
        print(f"  draws      pred {rep.pred_draw_rate:.2f} vs obs {rep.obs_draw_rate:.2f}")
        print(f"  fav hit    {rep.favourite_hit_rate:.2f}")
        print(f"  goals/game pred {rep.pred_avg_total_goals:.2f} vs obs {rep.obs_avg_total_goals:.2f}")
    for nnote in rep.notes:
        print(f"  • {nnote}")
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
