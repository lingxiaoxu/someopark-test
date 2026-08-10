"""research/shadow_pr2.py — PR-2 (#126): is the argmax defer-to-market filter earning its keep?

The registration, verbatim
--------------------------
    登记日 2026-08-05
    改动   `ops/decide_all._place_argmax` 的 `if st.fair > st.cost: return False`
    事前假设 无。这是一次**重新验证**,不是一次改进
    判据   前向 20 笔 argmax 腿:带过滤 vs 不带过滤(影子记录两条),ROI 差 >= 5pp 才算规则
           有用;否则规则应当去掉,因为它在丢交易而没有换来东西
    K      1

Why it is being re-validated rather than improved
-------------------------------------------------
The filter's stated justification is "dual-window validated 27W-2L". That measurement
predates #109 (the PIT gate rebuild) and #127 (the bucket devig fix), so it is not
evidence about the strategy that runs today — it is evidence about a strategy that no
longer exists. #126 also found the argmax leg paying a mean 83.6c for something that
settles 72.7% of the time, which is the opposite of what a defer-to-market rule is
supposed to leave you holding. Neither observation tells us the rule is wrong; both tell
us nobody currently knows. Hence a re-validation with no directional hypothesis.

**The rule stays ON while this runs.** Nothing here changes live behaviour.

What the two arms are
---------------------
`decide_all._place_argmax` writes one `shadow_argmax` row per argmax opportunity:

* `arm='placed'`   — `fair <= cost`, the filter allowed it, the trade really happened.
* `arm='deferred'` — `fair > cost`, the filter killed it; the trade is counterfactual.

    ROI(filter ON)  = over `placed` only          <- the trades the filter admitted
    ROI(filter OFF) = over `placed` + `deferred`  <- the superset, filter removed

Because `argmax_candidate` selects and only THEN is `defers_to_market` consulted, turning
the filter off never changes which structure is bought. The arms are nested, not disjoint,
and that is the honest construction: "no filter" really is "everything we did, plus the
things we skipped".

BOTH arms are hold-to-settlement counterfactuals — and the ON arm is NOT the book's
realised PnL
-----------------------------------------------------------------------------------
An earlier draft of this docstring called the ON arm "what the book actually did". That
was wrong, and it is retracted. `_place_argmax` places only when `fair <= cost`, i.e. only
at `net_edge <= 0`; `ops/exits` liquidates any position with `hold_edge < EXIT_EDGE`
(-0.06). So an argmax leg survives only inside the 6-cent strip [-0.06, 0], and anything
below it is opened and closed within the SAME refresh cycle. Measured on all four argmax
legs on the book to date: 3 of 4 round-tripped inside 120 seconds for -9.2% of stake
(#148). Those three never reached settlement at all.

This module deliberately scores both arms held to settlement anyway, because PR-2 asks a
question about the ENTRY filter, and the only way to isolate an entry rule is to hold exit
policy fixed across the arms. Holding it fixed at "hold to settlement" is a choice, and
this is it, stated in advance. The consequence must be read with the result:

    PR-2's ROI figures are NOT the book's realised PnL on the argmax stream, and the
    verdict is about the filter alone. The interaction with the exit rule is #148's
    question, not this registration's — resolving one does not resolve the other.

That is the #144 hazard (a scorer grading a strategy nobody runs) turned into a declared
assumption instead of a silent one.

Three things this module refuses to do
--------------------------------------
1. **No verdict below the registered n.** `PENDING` until `N_FORWARD` argmax legs have
   settled, and the ROI numbers below that are a progress readout. #128 is the precedent.
2. **No re-derivation of prices.** `legs_json` holds the fill prices as recorded at the
   moment of the decision. Settling from a book reconstructed after the outcome is known
   is a researcher degree of freedom, and it is the one that quietly turns a forward test
   into a fitted one.
3. **No second PnL formula.** Settlement goes through `strategy.edge.settle_struct`, the
   same function `research/walkforward.py` grades its own trades with. #141 is what the
   alternative costs.

`risk.check` is applied to BOTH arms inside `_place_argmax`, before the filter is
consulted, so a deferred trade that risk would have rejected anyway is never credited to
the no-filter arm. That is the same asymmetric-sample hazard `shadow_claims` handles by
intersecting its event sets.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

REGISTERED = "2026-08-05T00:00:00+00:00"
N_FORWARD = 20        # registered sample size: argmax legs, counted in the OFF arm
MIN_GAP_PP = 5.0      # registered bar: ROI(ON) - ROI(OFF) >= 5pp for the rule to be kept
K = 1


@dataclass(frozen=True)
class _Leg:
    """The three attributes `edge.settle_struct` needs, off a stored `legs_json` entry."""
    ticker: str
    side: str
    price: float


def _results(conn, series: str, period: str) -> dict:
    """{ticker -> 'yes'/'no'} for one settled event, empty when it has not settled."""
    return {r["ticker"]: r["result"] for r in conn.execute(
        "SELECT ticker, result FROM settlements WHERE series=? AND period=?"
        " AND result IN ('yes','no')", (series, period)).fetchall()}


def _rows(conn, asof: datetime) -> list[dict]:
    """Every settled `shadow_argmax` row recorded at or after the registration.

    Unsettled rows are not "pending zeroes" — they are simply not yet observations, and
    counting them would let the sample size be reached by opening trades rather than by
    resolving them.
    """
    out = []
    for r in conn.execute(
            "SELECT * FROM shadow_argmax WHERE ts_utc >= ? AND ts_utc <= ?"
            " ORDER BY ts_utc", (REGISTERED, asof.isoformat())).fetchall():
        res = _results(conn, r["series"], r["period"])
        if not res:
            continue
        legs = [_Leg(l["ticker"], l["side"], float(l["price"]))
                for l in json.loads(r["legs_json"])]
        from prediction_market_macro.strategy.edge import settle_struct
        realized = settle_struct(legs, int(r["count"]), res)
        if realized is None:
            continue                       # a leg of this structure has not settled
        out.append({"series": r["series"], "period": r["period"], "arm": r["arm"],
                    "ts": r["ts_utc"], "desc": r["desc"],
                    "fair": round(float(r["fair"]), 4),
                    "cost": round(float(r["cost"]), 4),
                    "staked": round(float(r["size_usd"]), 4),
                    "realized": round(float(realized), 4),
                    "won": realized > 0})
    return out


def _roi(rows: list[dict]) -> float | None:
    staked = sum(r["staked"] for r in rows)
    if staked <= 0:
        return None
    return sum(r["realized"] for r in rows) / staked


def _arm_stats(rows: list[dict]) -> dict:
    roi = _roi(rows)
    return {"n": len(rows),
            "staked": round(sum(r["staked"] for r in rows), 4),
            "realized": round(sum(r["realized"] for r in rows), 4),
            "n_won": sum(1 for r in rows if r["won"]),
            "win_rate": None if not rows else round(
                sum(1 for r in rows if r["won"]) / len(rows), 4),
            "mean_cost": None if not rows else round(
                sum(r["cost"] for r in rows) / len(rows), 4),
            "roi": None if roi is None else round(roi, 5)}


def run(conn, asof: datetime | None = None) -> dict:
    """The PR-2 readout. `PENDING` until `N_FORWARD` argmax legs have settled."""
    now = asof or datetime.now(timezone.utc)
    rows = _rows(conn, now)
    placed = [r for r in rows if r["arm"] == "placed"]
    deferred = [r for r in rows if r["arm"] == "deferred"]

    on, off = _arm_stats(placed), _arm_stats(rows)
    gap = (None if on["roi"] is None or off["roi"] is None
           else round((on["roi"] - off["roi"]) * 100, 2))

    out = {
        "registered": REGISTERED, "k": K,
        "asof": now.isoformat(),
        "n_forward": len(rows), "n_required": N_FORWARD,
        "n_placed": len(placed), "n_deferred": len(deferred),
        "filter_on": {"comparison": "placed only, held to settlement", **on},
        "filter_off": {"comparison": "placed + deferred, held to settlement", **off},
        "exit_policy_note": (
            "BOTH arms are held-to-settlement counterfactuals; neither is the book's "
            "realised PnL. `_place_argmax` enters only at net_edge <= 0 while `ops/exits` "
            "liquidates below -0.06, so a real argmax leg survives only in the 6c strip "
            "[-0.06, 0] — 3 of the first 4 round-tripped in the same cycle at -9.2% and "
            "never settled. Exit policy is held FIXED across the arms on purpose: PR-2 "
            "asks about the ENTRY filter, and that is the only way to isolate one. The "
            "entry/exit contradiction itself is #148, not this registration."),
        "roi_gap_pp": gap,
        "min_gap_pp": MIN_GAP_PP,
        "deferred_detail": deferred,
        "criterion": (
            f"PR-2, K={K}: {N_FORWARD} forward settled argmax legs from "
            f"{REGISTERED[:10]}. ROI(filter ON) - ROI(filter OFF) >= {MIN_GAP_PP}pp keeps "
            "the rule. Below that the rule is dropping trades without buying anything and "
            "should be removed. Note the asymmetry the registration intends: this is a "
            "re-validation with NO prior, so 'the rule is useless' is a real and "
            "actionable outcome, not a failure to find something."),
        "counting_note": (
            "`n_forward` counts legs in the OFF arm (placed + deferred), because that is "
            "the population the rule acts on and it is the only count under which both "
            "arms are non-empty. The registration says '前向 20 笔 argmax 腿' without "
            "naming an arm; this is the reading that makes the comparison possible, and "
            "it is written down here rather than chosen later."),
    }
    out["verdict"] = _verdict(out)
    return out


def _verdict(out: dict) -> str:
    if out["n_forward"] < out["n_required"]:
        return (f"PENDING — {out['n_forward']}/{out['n_required']} settled argmax legs "
                f"({out['n_placed']} placed, {out['n_deferred']} deferred). No verdict; "
                "the ROI figures above are a progress readout, not a result.")
    gap = out["roi_gap_pp"]
    if gap is None:
        return "no-data — one of the arms staked nothing, so no ROI can be formed."
    if gap >= out["min_gap_pp"]:
        return (f"KEEP — the filter is worth {gap:.2f}pp of ROI over "
                f"{out['n_forward']} legs (bar was {out['min_gap_pp']}pp). The rule "
                "stands, now on evidence about the current strategy rather than the "
                "pre-#109 one.")
    return (f"REMOVE — the filter bought {gap:.2f}pp, short of the registered "
            f"{out['min_gap_pp']}pp bar, while skipping {out['n_deferred']} of "
            f"{out['n_forward']} legs. Per the registration a rule that drops trades "
            "without paying for itself should be removed, and removing it is a change to "
            "`_place_argmax` that needs its own review — this module does not apply it.")


def main():
    import argparse

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    conn = init_db(a.db or load_settings().db_path)
    print(json.dumps(run(conn), ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
