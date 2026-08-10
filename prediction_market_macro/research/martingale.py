"""research/martingale.py — PR-7 step 0: is the book's price a martingale for settlement?

**This module produces no trading rule.** It answers the question that decides whether
writing one is worth the risk, and it is registered at K=0 for that reason (see
`docs/PREREGISTER.md`, PR-7).

The argument it implements
--------------------------
A binary contract's loss is already capped at 100% of the stake, so a stop-loss cannot
"control risk" here — the risk is bounded by the instrument, not by the rule. The only
way a price-triggered exit can add expected value is if the price is **not** a martingale
with respect to settlement: if a contract that has fallen keeps falling relative to what
it eventually pays, cutting is +EV; if the price is unbiased, then selling at any price is
expectation-neutral and the rule is a pure levy of one spread crossing plus two fees. On
this book that levy is large (§25.5: the bid-side path and the mid path differ by ~$3 on
$37 of stake), so "neutral" means "strictly worse".

So the test is on `e = y - m`, where `m` is the market's implied probability that the
structure pays and `y ∈ {0,1}` is whether it did. `walkforward._mtm_path` records `m` as
`m_mid` on exactly the scale `Struct.fill_cost` quotes `cost` on — for a bucket,
`mid_lo - mid_hi`, because one of its two legs pays in every branch and the position is
therefore a plain binary priced at that difference.

Three pre-registered readings, and only these three:
  all        E[e]                                     — is the book biased at all
  drawdown   E[e | m <= entry_m - DROP]                — does a loser keep losing (⇒ S1)
  rally      E[e | m >= entry_m + DROP]                — does a winner keep winning

`DROP = 0.10` is not fitted: it is the smallest move that clears a typical one-way spread
on this book, so anything below it is a quote artefact rather than a move.

Why the CI is clustered
-----------------------
Eight days of one position are not eight observations — they share one settlement, and
two positions on the same (series, period) share it too. #129 measured the damage from
getting this wrong: unclustered intervals came out about 6x too tight. The bootstrap here
resamples **events**, so a whole event's rows travel together.

Read the power line before the estimate
---------------------------------------
`power_note` reports the CI half-width this sample can deliver. PR-7 pre-registered that
~46 events give roughly ±13pp, which means **this data can only detect a large bias**. A
CI that straddles zero is "underpowered", not "no effect", and PR-7's answer to it is to
close #140 rather than to pick a threshold anyway.
"""
from __future__ import annotations

import json
import random
from statistics import fmean

DROP = 0.10           # see module docstring — a spread-width move, not a fitted knob
N_BOOT = 5000
SEED = 20260805       # fixed so two runs of the same data give the same interval


def load_paths(conn, source_hash: str, stream: str = "hybrid") -> list[dict]:
    """The §25.5 held paths of one stored walk-forward, restricted to one stream.

    `held_paths` carries BOTH streams (`_trade_row` runs for each), and the streams
    overlap on events where only one fired. PR-7 registered the `hybrid` population —
    the live rule — so the trade list of that stream is what selects the keys here.
    """
    feat = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='walkforward_features'"
        " AND config_hash=?", (source_hash,)).fetchone()
    head = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='daily_walkforward'"
        " AND config_hash=?", (source_hash,)).fetchone()
    if feat is None or head is None:
        raise LookupError(f"no stored walk-forward with config_hash={source_hash!r}")
    paths = json.loads(feat["metrics_json"])["held_paths"]
    st = (json.loads(head["metrics_json"]).get("streams") or {}).get(stream)
    if st is None:
        raise LookupError(f"run has no {stream!r} stream")
    trades = st.get("trades") or []
    if st.get("n_trades") is not None and len(trades) != st["n_trades"]:
        raise ValueError(
            f"{stream!r} carries {len(trades)} of {st['n_trades']} trades — the source "
            "capped its list, so this would silently test a subsample")
    out = []
    for t in trades:
        rows = paths.get(f"{t['series']}/{t['period']}/{t['desc']}")
        if not rows:                       # opened too close to settlement to have a path
            continue
        out.append({"event": f"{t['series']}/{t['period']}", "series": t["series"],
                    "desc": t["desc"], "rows": rows})
    return out


def _obs(paths: list[dict]) -> list[tuple[str, float, float, float]]:
    """(event, e, m, entry_m) per marked day, dropping rows PR-7 cannot score.

    A row is unscorable when the structure never settled (`settle_y is None`, e.g. a
    contract Kalshi voided) or when the run predates the fields. Both are dropped rather
    than imputed, and `n_dropped` is reported so a mostly-dropped sample cannot read as a
    clean null.
    """
    out = []
    for p in paths:
        for r in p["rows"]:
            y, m, m0 = r.get("settle_y"), r.get("m_mid"), r.get("entry_m")
            if y is None or m is None or m0 is None:
                continue
            out.append((p["event"], float(y) - float(m), float(m), float(m0)))
    return out


def _boot_ci(obs, rng: random.Random, n_boot: int = N_BOOT) -> tuple[float, float]:
    """Event-clustered percentile bootstrap of the mean of `e`."""
    by_event: dict[str, list[float]] = {}
    for ev, e, _m, _m0 in obs:
        by_event.setdefault(ev, []).append(e)
    events = list(by_event)
    if len(events) < 2:
        return (float("nan"), float("nan"))
    means = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(events)):
            pool.extend(by_event[events[rng.randrange(len(events))]])
        means.append(fmean(pool))
    means.sort()
    return (means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1])


def _cell(obs, rng, label: str) -> dict:
    if not obs:
        return {"label": label, "n_rows": 0, "n_events": 0, "note": "empty cell"}
    events = {ev for ev, _e, _m, _m0 in obs}
    e = [x[1] for x in obs]
    lo, hi = _boot_ci(obs, rng)
    return {"label": label, "n_rows": len(e), "n_events": len(events),
            "mean_e": round(fmean(e), 5),
            "mean_m": round(fmean([x[2] for x in obs]), 5),
            "ci95": [round(lo, 5), round(hi, 5)],
            "crosses_zero": not (lo > 0 or hi < 0)}


def run(conn, source_hash: str, stream: str = "hybrid", drop: float = DROP) -> dict:
    """PR-7 step 0. Returns the three registered cells plus the power statement."""
    rng = random.Random(SEED)
    paths = load_paths(conn, source_hash, stream=stream)
    obs = _obs(paths)
    n_rows_total = sum(len(p["rows"]) for p in paths)
    cells = {
        "all": _cell(obs, rng, "all marked days"),
        "drawdown": _cell([o for o in obs if o[2] <= o[3] - drop], rng,
                          f"market fell >= {drop:.2f} from entry mid"),
        "rally": _cell([o for o in obs if o[2] >= o[3] + drop], rng,
                       f"market rose >= {drop:.2f} from entry mid"),
    }
    n_ev = cells["all"].get("n_events", 0)
    half = (0.45 / n_ev ** 0.5) * 1.96 if n_ev else None
    verdict = ("drawdown cell rejects: losers keep losing ⇒ PR-7 step 1 rule S1"
               if not cells["drawdown"].get("crosses_zero", True)
               else "rally cell rejects: PR-7 step 1, direction decides S2 vs hold"
               if not cells["rally"].get("crosses_zero", True)
               else "NEITHER cell rejects ⇒ PR-7 closes #140. No price-triggered exit "
                    "rule is registered, and none may be fitted on this window.")
    return {
        "source": source_hash, "stream": stream, "drop": drop,
        "n_paths": len(paths), "n_rows_scored": len(obs),
        "n_rows_dropped": n_rows_total - len(obs),
        "cells": cells,
        "power_note": (
            None if half is None else
            f"{n_ev} events ⇒ 95% CI half-width ≈ ±{half * 100:.1f}pp on the mean of "
            f"y-m. A cell that straddles zero inside that band is UNDERPOWERED, not a "
            f"measured null (PR-7 step 0 pre-registered this reading)."),
        "verdict": verdict,
        "note": "PR-7 step 0, K=0 — a diagnostic, not a rule. e = y - m_mid; m_mid is "
                "the market's implied probability on Struct.fill_cost's scale; CIs are "
                "event-clustered percentile bootstrap (#129).",
    }


def main():
    import argparse

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="config_hash of the daily_walkforward run to test")
    ap.add_argument("--stream", default="hybrid")
    ap.add_argument("--drop", type=float, default=DROP,
                    help="PR-7 registered 0.10. Changing it re-opens the registration.")
    a = ap.parse_args()
    conn = init_db(load_settings().db_path)
    print(json.dumps(run(conn, a.source, stream=a.stream, drop=a.drop),
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
