"""ops/backtest_export.py — honest OOS backtest for the frontend.

Point-in-time comparison on the last 60 days of completed matches across our
12 club competitions of:
  * our MODEL (Dixon-Coles, per-league PIT strength as of each kickoff),
  * the sharp BOOKMAKER consensus (de-vigged closing odds),
  * the UNIFORM baseline (1/3 each),
scored by Brier (= multiclass MSE vs the one-hot result), plus the model-vs-book
"blend" curve, the actual draw rate, and the best param set from param_sweep.

The point is transparency: the frontend shows WHY the discipline gate blocks
trading — not because of a bug, but because the early tournament has been
near-random (both model and market sit above the uniform Brier).

    python -m prediction_market_soccer.ops.backtest_export  →  data/output/backtest.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from prediction_market_soccer.config import CONFIG

_FINISHED = ("FT", "AET", "PEN")


def _brier(p, y):
    oh = [1.0 if k == y else 0.0 for k in range(3)]
    return sum((p[k] - oh[k]) ** 2 for k in range(3))


def build(conn=None) -> dict:
    """CLUB EDITION PIT rewrite: the WC version priced every settled match with
    TODAY's global model — valid there (ratings frozen pre-tournament) but
    look-ahead here, where ratings/blends refit daily on the very results being
    scored. Now every match is priced with its own per-(day, league) point-in-time
    model (performance_report._pit_strength — the same construction the bets use)
    and an EXPANDING PIT calibration (fit only on matches before its kickoff).
    Window: our leagues, last 60 days (aligned with performance_report)."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.match_pricing import is_knockout, price_match
    from prediction_market_soccer.model.probability_calibration import apply_calibration, fit_calibration
    from prediction_market_soccer.ops.performance_report import _comp_key, _pit_strength

    conn = conn or store.init_db()
    name = {t.team_id: t.name for t in load_prior().teams}
    cmap = {r["api_id"]: r["canonical_team_id"] for r in conn.execute(
        "SELECT api_id, canonical_team_id FROM team_meta WHERE canonical_team_id IS NOT NULL")}

    from prediction_market_soccer.config.leagues import neutral_venue_for, active as _active
    _lids = tuple(c.api_football_id for c in _active())
    rows = conn.execute(
        "SELECT f.api_id, f.home_api_id, f.away_api_id, f.home_goals, f.away_goals, f.raw_json, "
        "       f.kickoff_ts, f.round, f.league_id, "
        "       AVG(o.p_home) bh, AVG(o.p_draw) bd, AVG(o.p_away) ba, COUNT(DISTINCT o.bookmaker) nbk "
        "FROM fixture f LEFT JOIN match_odds o ON o.fixture_api_id=f.api_id "
        "AND o.bookmaker <> 'live_consensus' "   # pre-match book only (see note)
        "WHERE f.status_short IN ({}) AND f.home_goals IS NOT NULL "
        "AND f.league_id IN ({}) AND f.kickoff_ts >= datetime('now', '-60 days') "
        "GROUP BY f.api_id ORDER BY f.kickoff_ts".format(
            ",".join("?" * len(_FINISHED)), ",".join("?" * len(_lids))),
        (*_FINISHED, *_lids)).fetchall()
    from prediction_market_soccer.util.pricing import reg_score

    lab = ["H", "D", "A"]
    matches = []
    sum_model = sum_model_raw = sum_book = 0.0
    n = n_book = draws = book_hit = model_hit = 0
    weights = [0.0, 0.25, 0.5, 0.75, 1.0]
    blend_sum = {w: 0.0 for w in weights}
    _day_cache: dict = {}      # PIT strength per (kickoff date, comp)
    hist_P: list = []          # raw PIT probs of PRIOR matches (for the expanding calibration)
    hist_Y: list = []
    _cal_cache = {"n": -1, "cal": None}

    for r in rows:
        hi, ai = cmap.get(r["home_api_id"]), cmap.get(r["away_api_id"])
        if not (hi and ai) or not r["kickoff_ts"]:
            continue
        gh, ga = reg_score(r["raw_json"], r["home_goals"], r["away_goals"])  # 90' result
        y = 0 if gh > ga else (1 if gh == ga else 2)
        _lg = _comp_key(r["league_id"])
        _k = (r["kickoff_ts"][:10], _lg)
        if _k not in _day_cache:
            _day_cache[_k] = _pit_strength(conn, r["kickoff_ts"], _lg)
        ko = is_knockout(r["round"], _lg)
        raw = price_match(_day_cache[_k], hi, ai, knockout=False, host_neutral=neutral_venue_for(_lg, r["round"], conn, r["api_id"]))
        praw = [raw.p_home, raw.p_draw, raw.p_away]
        # expanding PIT calibration: refit only when history grew (matches share fits)
        if _cal_cache["n"] != len(hist_Y):
            _cal_cache = {"n": len(hist_Y),
                          "cal": fit_calibration(hist_P, hist_Y) if len(hist_Y) >= 3 else None}
        pm = apply_calibration(praw, _cal_cache["cal"], knockout=False)
        hist_P.append(praw)
        hist_Y.append(y)
        n += 1
        draws += (y == 1)
        sum_model += _brier(pm, y)
        sum_model_raw += _brier([raw.p_home, raw.p_draw, raw.p_away], y)
        mf = max(range(3), key=lambda k: pm[k])
        model_hit += (mf == y)
        row = {"home": name.get(hi, hi), "away": name.get(ai, ai), "league": _lg,
               "score": f"{gh}-{ga}", "result": lab[y],
               "model_pick": lab[mf], "model_p": round(pm[mf], 3)}
        if r["bh"] is not None:
            s = r["bh"] + r["bd"] + r["ba"]
            pb = [r["bh"] / s, r["bd"] / s, r["ba"] / s]
            sum_book += _brier(pb, y); n_book += 1
            bf = max(range(3), key=lambda k: pb[k])
            book_hit += (bf == y)
            for w in weights:
                blend_sum[w] += _brier([(1 - w) * pm[k] + w * pb[k] for k in range(3)], y)
            row.update({"book_pick": lab[bf], "book_p": round(pb[bf], 3), "n_book": r["nbk"]})
        matches.append(row)

    uniform = round(2 / 3, 4)
    model_brier = round(sum_model / n, 4) if n else None          # calibrated (live)
    model_raw_brier = round(sum_model_raw / n, 4) if n else None   # before calibration
    book_brier = round(sum_book / n_book, 4) if n_book else None
    blend_curve = [{"w": w, "brier": round(blend_sum[w] / n_book, 4)} for w in weights] if n_book else []
    trade_grade = bool(model_brier is not None and model_brier <= uniform)

    sweep = None
    sp = CONFIG.paths.output / "param_sweep.json"
    if sp.exists():
        try:
            d = json.loads(sp.read_text(encoding="utf-8"))
            sweep = {"best_params": d["best"]["params"], "best_brier": round(d["best"]["brier"], 4),
                     "n_param_sets": d["n_param_sets"]}
        except Exception:
            pass

    # The book clause only appears when a book number actually exists. API-Football
    # returns no pre-match odds for these club fixtures (results=0, no error), so
    # n_with_book is 0 and the old text claimed we beat a book that was never there.
    _book_clause = (f" and below the sharp book ({book_brier})" if n_book else
                    " (no pre-match bookmaker odds are available for these club "
                    "competitions, so there is no book column to compare against)")
    conclusion = (
        f"On {n} settled matches (draw rate {draws}/{n}, {round(100*draws/n) if n else 0}%): the RAW "
        f"model was over-confident (Brier {model_raw_brier}), but after point-in-time probability "
        f"calibration the CALIBRATED model scores {model_brier} — below the uniform baseline "
        f"{uniform}{_book_clause}. Every match is priced with its own as-of-kickoff model."
        if (n and trade_grade) else
        (f"On {n} settled matches the calibrated model Brier {model_brier} is not yet below uniform "
         f"{uniform} — gate stays blocked." if n else "No settled matches yet.")
    )

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        # {key, args} so the five-language frontend can render this sentence itself;
        # `conclusion` stays as the English fallback and as what a JSON reader sees.
        "conclusion_i18n": {
            "key": "backtest.tradeGrade" if (n and trade_grade) else "backtest.blocked",
            "args": {"n": n, "drawPct": round(100 * draws / n) if n else 0,
                     "raw": model_raw_brier, "model": model_brier,
                     "uniform": uniform, "book": book_brier if n_book else None},
        },
        "note_key": "notes.backtest",
        "n_settled": n, "n_with_book": n_book,
        "draw_rate": round(draws / n, 3) if n else None,
        "trade_grade": trade_grade,
        "brier": {"model": model_brier, "model_raw": model_raw_brier, "book": book_brier, "uniform": uniform},
        "accuracy": {"model_fav_hit": f"{model_hit}/{n}", "book_fav_hit": f"{book_hit}/{n_book}"},
        "blend_curve": blend_curve,
        "param_sweep": sweep,
        "matches": matches,
        "conclusion": conclusion,
    }


def main() -> None:
    doc = build()
    CONFIG.paths.ensure()
    (CONFIG.paths.output / "backtest.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    b = doc["brier"]
    print(f"BACKTEST — {doc['n_settled']} settled ({doc['n_with_book']} w/ book), draw rate {doc['draw_rate']}")
    print(f"  Brier  model={b['model']}  book={b['book']}  uniform={b['uniform']}")
    print(f"  fav-hit model={doc['accuracy']['model_fav_hit']}  book={doc['accuracy']['book_fav_hit']}")
    print(f"  blend curve: {[(c['w'], c['brier']) for c in doc['blend_curve']]}")
    print(f"  {doc['conclusion']}")


if __name__ == "__main__":
    main()
