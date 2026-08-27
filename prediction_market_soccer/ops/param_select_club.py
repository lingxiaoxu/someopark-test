"""ops/param_select_club.py — per-league, time-split parameter selection (club edition).

The WC param_sweep evaluated ONE GLOBAL model (merged prior, single cfg.base_mu),
which is not the model this system trades: production prices every match with a
PER-LEAGUE model (per-comp fitted base_mu/home_adv + per-comp prior, C2). This
harness scores candidate parameter sets the way production actually prices:

  * candidates:  DEF   — current config defaults (what is live today),
                 WC    — the World-Cup-selected set (../prediction_market
                         param_selected: beta .70, dc_rho -.12, squad 0, ...),
                 REFIT — staged grid search on club data (structural beta/dc_rho
                         first, then greedy blend weights), trained on TRAIN only;
  * data:        season-2026 settled matches of our 12 comps;
  * split:       TEST = the last N days (default 14), TRAIN = everything before —
                 selection/calibration happen on TRAIN, the reported comparison
                 on TEST (out-of-sample in time);
  * scoring:     Brier on the 90-minute 3-way, with each candidate's own
                 calibration FIT ON TRAIN and applied unchanged to TEST;
  * honesty:     the club prior's current-table anchor (played>=8) and today's
                 anchor indices contain some post-TRAIN information for the SA
                 leagues (disclosed in the output; affects all candidates alike);
                 blend indices for TEST scoring are cut as-of the TEST start.

Adoption: if a non-DEF candidate beats DEF's pooled calibrated TEST Brier by more
than --adopt-margin (default 0.005), param_selected.json is written (config
auto-loads it → production switches). Otherwise report-only.

    python -m prediction_market_soccer.ops.param_select_club [--test-days 14]
        [--adopt-margin 0.005] [--dry-run]
    → data/output/param_select_club.json (+ param_selected.json on adoption)
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import active

_FINISHED = ("FT", "AET", "PEN")

# The WC-selected set (prediction_market/data/output/param_selected.json,
# n=104 WC matches). base_mu is NOT transplanted: club base_mu/home_adv are
# per-league fitted (C2) and must not be overridden by a WC global.
WC_PARAMS = {
    "beta": 0.70, "dc_rho": -0.12,
    "fc_blend_weight": 0.12, "squad_blend_weight": 0.0, "form_blend_weight": 0.10,
    "oppadj_def_weight": 0.45, "oppadj_off_weight": 0.25,
}

_SWEPT = ("beta", "dc_rho", "fc_blend_weight", "squad_blend_weight",
          "form_blend_weight", "oppadj_def_weight", "oppadj_off_weight")


def _brier(p, y):
    oh = [1.0 if k == y else 0.0 for k in range(3)]
    return sum((p[k] - oh[k]) ** 2 for k in range(3))


def load_matches(conn):
    """Per-league season-2026 settled matches: {comp_key: [(hi, ai, y, kickoff, ko)]}."""
    from prediction_market_soccer.config.leagues import neutral_venue_for
    from prediction_market_soccer.util.pricing import reg_score
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}
    comp_of = {c.api_football_id: c.key for c in active()}
    lids = tuple(comp_of)
    rows = conn.execute(
        "SELECT api_id, home_api_id, away_api_id, home_goals, away_goals, raw_json, "
        "kickoff_ts, round, league_id, season FROM fixture "
        "WHERE status_short IN ({}) AND home_goals IS NOT NULL AND season IN (2025, 2026) "
        "AND league_id IN ({}) ORDER BY kickoff_ts".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(lids))),
        (*_FINISHED, *lids)).fetchall()
    out: dict[str, list] = {}
    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai) or not r["kickoff_ts"]:
            continue
        lg = comp_of[r["league_id"]]
        gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])
        y = 0 if gh > ga else (1 if gh == ga else 2)
        out.setdefault(lg, []).append(
            (hi, ai, y, r["kickoff_ts"],
             # NEUTRAL venue, which is what pricing needs — not "is this a knockout".
             # Both legs of a two-legged tie have a host; only a neutral final does not.
             neutral_venue_for(lg, r["round"], conn, r["api_id"]),
             r["season"]))
    return out


def _anchor_indices(conn, as_of: str | None):
    """squad/form/fc z-indices, form cut at ``as_of`` (PIT for the TEST window)."""
    idx = {}
    try:
        from prediction_market_soccer.model.squad_strength import squad_index
        idx["squad"] = squad_index(conn)
    except Exception:
        idx["squad"] = None
    try:
        from prediction_market_soccer.model.form_strength import form_index
        idx["form"] = form_index(conn, as_of=as_of)
    except Exception:
        idx["form"] = None
    try:
        from prediction_market_soccer.model.fc_strength import fc_squad_index
        idx["fc"] = fc_squad_index(conn)
    except Exception:
        idx["fc"] = None
    return idx


class _Evaluator:
    """Prices per-league matches under a candidate cfg; caches priors and models."""

    def __init__(self, conn, matches, idx_train, idx_test, cutoff):
        from prediction_market_soccer.ingest.club_prior import load_prior
        self.conn = conn
        self.matches = matches
        self.idx_train, self.idx_test = idx_train, idx_test
        self.cutoff = cutoff
        # A 2025 match must be priced with the prior that was knowable THEN — the one
        # built from the 2024 tables (clubs_<comp>_s2025.json, written by
        # club_prior.build_all(season=2025)). Using today's prior on last season's
        # results would hand the model the answer sheet.
        def _prior(lg, season):
            if season >= 2026:
                return load_prior(lg)
            try:
                return load_prior(f"{lg}_s2025")
            except Exception:
                return load_prior(lg)
        self.priors = {(lg, s): _prior(lg, s) for lg in matches for s in (2025, 2026)}
        self.altdata_cache: dict = {}

    def _league_model(self, lg, cfg, idx, season=2026):
        from prediction_market_soccer.model.fc_strength import fc_adjusted_ratings
        from prediction_market_soccer.model.form_strength import form_adjusted_ratings
        from prediction_market_soccer.model.squad_strength import squad_adjusted_ratings
        from prediction_market_soccer.model.strength import build_strength
        sm = build_strength(self.priors[(lg, season)], cfg)
        if cfg.squad_blend_weight and idx.get("squad"):
            sm = squad_adjusted_ratings(sm, idx["squad"], cfg.squad_blend_weight)
        if cfg.form_blend_weight and idx.get("form"):
            sm = form_adjusted_ratings(sm, idx["form"], cfg.form_blend_weight)
        if cfg.fc_blend_weight and idx.get("fc"):
            sm = fc_adjusted_ratings(sm, idx["fc"], cfg.fc_blend_weight)
        if cfg.oppadj_def_weight or cfg.oppadj_off_weight:
            from prediction_market_soccer.model.altdata_adjust import altdata_index
            ck = (lg, "train" if idx is self.idx_train else "test")
            if ck not in self.altdata_cache:
                as_of = None if idx is self.idx_train else self.cutoff
                self.altdata_cache[ck] = altdata_index(self.conn, sm.ratings, as_of=as_of)
            sm = replace(sm, adj=self.altdata_cache[ck])
        return sm

    def probs(self, params: dict, split: str):
        """[(p_vec, y)] over the given split ('train'|'test') under ``params``."""
        from prediction_market_soccer.model.match_pricing import price_match
        cfg = replace(CONFIG.model, **params)
        idx = self.idx_train if split == "train" else self.idx_test
        out = []
        for lg, ms in self.matches.items():
            rows = [m for m in ms if (m[3] < self.cutoff) == (split == "train")]
            if not rows:
                continue
            for season in (2025, 2026):
                srows = [m for m in rows if m[5] == season]
                if not srows:
                    continue
                sm = self._league_model(lg, cfg, idx, season)
                out.extend(self._price(sm, srows, lg))
        return out

    def _price(self, sm, rows, lg):
        from prediction_market_soccer.model.match_pricing import price_match
        out = []
        if True:
            for hi, ai, y, _ko_ts, neutral, _season in rows:
                if not (hi in sm.ratings and ai in sm.ratings):
                    continue
                if not (hi in sm.ratings and ai in sm.ratings):
                    continue
                mp = price_match(sm, hi, ai, knockout=False, host_neutral=neutral)
                out.append(((mp.p_home, mp.p_draw, mp.p_away), y, lg))
        return out


def _score(recs, cal=None):
    from prediction_market_soccer.model.probability_calibration import apply_calibration
    if not recs:
        return {"n": 0, "brier": None, "acc": None}
    b = hits = 0.0
    per_lg: dict[str, list] = {}
    for p, y, lg in recs:
        pv = apply_calibration(list(p), cal, knockout=False) if cal else list(p)
        b += _brier(pv, y)
        hits += 1 if max(range(3), key=lambda k: pv[k]) == y else 0
        per_lg.setdefault(lg, []).append(_brier(pv, y))
    return {"n": len(recs), "brier": round(b / len(recs), 4),
            "acc": round(hits / len(recs), 4),
            "per_league": {lg: {"n": len(v), "brier": round(sum(v) / len(v), 4)}
                           for lg, v in sorted(per_lg.items())}}


def _fit_cal(recs):
    from prediction_market_soccer.model.probability_calibration import fit_calibration
    if len(recs) < 3:
        return None
    return fit_calibration([list(p) for p, _y, _lg in recs], [y for _p, y, _lg in recs])


def refit(ev: _Evaluator) -> dict:
    """Staged grid: structural beta × dc_rho (defaults blends), then greedy blends —
    all selection on TRAIN calibrated Brier only."""
    def train_brier(params):
        recs = ev.probs(params, "train")
        cal = _fit_cal(recs)
        return _score(recs, cal)["brier"], cal

    base = {k: getattr(CONFIG.model, k) for k in _SWEPT}
    best_p, (best_b, _) = dict(base), (float("inf"), None)
    for beta in (0.40, 0.55, 0.70, 0.85):
        for rho in (-0.16, -0.12, -0.08, -0.05, 0.0):
            p = dict(base, beta=beta, dc_rho=rho)
            b, _c = train_brier(p)
            if b is not None and b < best_b:
                best_p, best_b = p, b
    for key, vals in (("fc_blend_weight", (0.0, 0.12, 0.24)),
                      ("squad_blend_weight", (0.0, 0.15)),
                      ("form_blend_weight", (0.0, 0.10, 0.20)),
                      ("oppadj_def_weight", (0.0, 0.25, 0.45)),
                      ("oppadj_off_weight", (0.0, 0.25))):
        for v in vals:
            if v == best_p[key]:
                continue
            p = dict(best_p, **{key: v})
            b, _c = train_brier(p)
            if b is not None and b < best_b:
                best_p, best_b = p, b
    return {"params": best_p, "train_brier": best_b}


def run(test_days: int = 14, adopt_margin: float = 0.005, dry_run: bool = False) -> dict:
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=test_days)).isoformat(timespec="seconds")
    matches = load_matches(conn)
    n_all = sum(len(v) for v in matches.values())
    n_test = sum(1 for v in matches.values() for m in v if m[3] >= cutoff)
    print(f"[param_select] {n_all} settled season-2026 matches, TEST(last {test_days}d)={n_test}")

    idx_train = _anchor_indices(conn, as_of=None)     # anchors for TRAIN scoring
    idx_test = _anchor_indices(conn, as_of=cutoff)    # PIT: cut at TEST start
    ev = _Evaluator(conn, matches, idx_train, idx_test, cutoff)

    candidates = {
        "DEF": {k: getattr(CONFIG.model, k) for k in _SWEPT},
        "WC": dict(WC_PARAMS),
    }
    rf = refit(ev)
    candidates["REFIT"] = rf["params"]

    report = {"ts": datetime.now(timezone.utc).isoformat(),
              "cutoff": cutoff, "test_days": test_days,
              "n_matches": n_all, "n_test": n_test,
              "refit_train_brier": rf["train_brier"],
              "candidates": {}, "disclosure": (
                  "Club priors carry a current-table anchor (played>=8) and squad/fc "
                  "indices are computed on today's data — for the SA leagues this leaks "
                  "some post-TRAIN information into all candidates equally. Form/altdata "
                  "indices for TEST scoring are cut at the TEST start (PIT).")}
    for cname, params in candidates.items():
        train_recs = ev.probs(params, "train")
        cal = _fit_cal(train_recs)
        test_recs = ev.probs(params, "test")
        report["candidates"][cname] = {
            "params": params,
            "train": _score(train_recs, cal), "test": _score(test_recs, cal),
            "test_raw": _score(test_recs, None),
            "calibration": {"method": cal.get("method"), "param": cal.get("param"),
                            "draw_boost": cal.get("draw_boost")} if cal else None,
        }
        t = report["candidates"][cname]["test"]
        print(f"  {cname:6s} test brier={t['brier']} acc={t['acc']} (n={t['n']})")

    briers = {c: r["test"]["brier"] for c, r in report["candidates"].items()
              if r["test"]["brier"] is not None}
    winner = min(briers, key=briers.get) if briers else None
    report["winner"] = winner
    adopt = (winner is not None and winner != "DEF"
             and briers[winner] <= briers.get("DEF", math.inf) - adopt_margin)
    report["adopted"] = bool(adopt) and not dry_run

    CONFIG.paths.ensure()
    (CONFIG.paths.output / "param_select_club.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if adopt and not dry_run:
        sel = {"params": candidates[winner],
               "brier": briers[winner], "n_settled": n_test,
               "selection": {"method": f"club per-league time-split (TEST last {test_days}d), "
                                       f"winner {winner} by pooled calibrated test Brier",
                             "margin_vs_DEF": round(briers.get("DEF", math.nan) - briers[winner], 4),
                             "source": "ops/param_select_club.py"},
               "ts": report["ts"]}
        (CONFIG.paths.output / "param_selected.json").write_text(
            json.dumps(sel, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[param_select] ADOPTED {winner} → param_selected.json (production auto-loads)")
    else:
        print(f"[param_select] winner={winner}; no adoption "
              f"({'dry-run' if dry_run else 'margin not met or DEF wins'})")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-days", type=int, default=14)
    ap.add_argument("--adopt-margin", type=float, default=0.005)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(test_days=a.test_days, adopt_margin=a.adopt_margin, dry_run=a.dry_run)


if __name__ == "__main__":
    main()
