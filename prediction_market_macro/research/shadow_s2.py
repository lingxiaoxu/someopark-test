"""research/shadow_s2.py — PR-7 step 1 (#143): score the S2 shadow against the live arm.

`ops/exits.shadow_run` writes what S2 would have done, every cycle, without executing it.
This module reads those records back once the positions have closed and reports the two
arms plus the pre-registered criterion. It decides nothing on its own — the verdict text
is a lookup on numbers the registration fixed in advance.

The two arms
------------
live  the ledger as it happened: rule 1 at `EXIT_EDGE = -0.06`, plus the red-light and
      regime-review exits, plus settlement. Free — it is what the book did.
S2    the same book with rule 1 tightened to `hold_edge <= 0`. A trade's S2 arm is its
      FIRST `triggered` shadow row at or before the live close; if S2 never fired before
      the position closed, the two arms are the same trade and contribute identically.

So S2 can only ever exit **earlier** than live, never later. Trades where it never fires
are not dropped: they are real evidence that the rule is inert, and dropping them would
inflate whatever the firing subset happens to show.

What is NOT re-derived here
---------------------------
Both arms' dollar PnL come from `ops.exits.exit_realized` / the ledger's own
`realized_usd`. This module never re-prices a leg. That is deliberate: the moment a
scorer re-prices, it can disagree with the thing it is scoring, and #141 is the standing
example of two paths quietly pricing the same object differently.

Reading the result
------------------
PR-7 step 1 registered: **30 forward hybrid trades, realized-ROI gap >= 5pp, event-
clustered 95% CI on the gap not crossing zero.** Below 30 trades there is no verdict —
`run()` returns `PENDING` and the ROI numbers in it are a progress readout, not a result.
Reporting a gap at n=7 as though it meant something is exactly the failure #128 was.

Clustering is by `(series, period)` because two positions on the same event share one
settlement; #129 measured unclustered intervals coming out ~6x too tight.
"""
from __future__ import annotations

import json
import random

REGISTERED = "2026-08-05T00:00:00+00:00"   # PR-7 step 1 registration; forward count starts here
N_FORWARD = 30                             # registered sample size — not reachable early
MIN_ROI_GAP = 0.05                         # registered effect size
N_BOOT = 5000
SEED = 20260806                            # fixed so two runs give the same interval

OPEN_KINDS = ("open", "argmax", "arb", "snipe")
CLOSE_KINDS = ("exit", "cancel", "settle_note")


def _in(kinds) -> str:
    """`IN (...)` over a fixed constant tuple — built by hand rather than by repr() so a
    one-element tuple doesn't emit SQLite-invalid `('open',)`."""
    return "(" + ",".join(f"'{k}'" for k in kinds) + ")"


def _realized(row) -> float | None:
    """The ledger's own realized PnL for a closing row, or None if it carries none.

    `cancel` rows (the #121 retirement sweep) never carry one — they retire a position
    administratively rather than closing it in the market. Exit rows written before the
    #141 fix don't either. Both are unscorable and must be DROPPED, never imputed as 0:
    a zero would read as a flat trade rather than as missing data.
    """
    try:
        v = json.loads(row["inputs_json"] or "{}").get("realized_usd")
    except (TypeError, ValueError):
        return None
    return None if v is None else float(v)


def load_trades(conn, since: str = REGISTERED) -> dict:
    """Forward hybrid trades since the registration, each with both arms priced.

    The ledger IS the hybrid stream — `decide_all` has already applied the live
    per-(series, period) choice between the edge trade and the favourite, so every
    open/argmax row here is one hybrid trade. Re-entries after an exit are separate
    trades, which is what the registered "30 trades" counts.
    """
    opens = conn.execute(
        f"SELECT * FROM decisions WHERE kind IN {_in(OPEN_KINDS)} AND ts_utc >= ?"
        " ORDER BY id", (since,)).fetchall()
    trades, still_open, unscorable = [], 0, []
    for d in opens:
        close = conn.execute(
            f"SELECT * FROM decisions WHERE series=? AND period=? AND kind IN"
            f" {_in(CLOSE_KINDS)} AND id>? ORDER BY id LIMIT 1",
            (d["series"], d["period"], d["id"])).fetchone()
        if close is None:
            still_open += 1
            continue
        live = _realized(close)
        if live is None:
            unscorable.append({"decision_id": d["id"], "close_kind": close["kind"],
                               "reason": "closing row carries no realized_usd"})
            continue
        # S2's arm: the first cycle it would have fired, at or before the live close.
        sh = conn.execute(
            "SELECT ts_utc, hold_edge, realized_usd FROM shadow_exits WHERE rule='S2'"
            " AND decision_id=? AND triggered=1 AND ts_utc<=? ORDER BY ts_utc LIMIT 1",
            (d["id"], close["ts_utc"])).fetchone()
        trades.append({
            "decision_id": d["id"], "event": f"{d['series']}/{d['period']}",
            "series": d["series"], "period": d["period"], "kind": d["kind"],
            "opened": d["ts_utc"], "closed": close["ts_utc"],
            "close_kind": close["kind"],
            "staked": float(d["size_usd"] or 0.0),
            "live_realized": live,
            "s2_realized": live if sh is None else float(sh["realized_usd"]),
            "s2_fired": sh is not None,
            "s2_ts": None if sh is None else sh["ts_utc"],
            "s2_hold_edge": None if sh is None else sh["hold_edge"],
        })
    return {"trades": trades, "n_open": still_open, "unscorable": unscorable}


def _roi(trades, arm: str) -> float | None:
    staked = sum(t["staked"] for t in trades)
    return None if staked <= 0 else sum(t[arm] for t in trades) / staked


def _boot_ci_gap(trades, rng: random.Random, n_boot: int = N_BOOT):
    """Event-clustered percentile bootstrap of (S2 ROI - live ROI).

    ROI is a ratio, so the gap is recomputed inside each resample rather than averaged
    from per-trade differences — resampling the numerator and denominator together is the
    only way the interval reflects how the ratio actually moves.
    """
    by_event: dict[str, list[dict]] = {}
    for t in trades:
        by_event.setdefault(t["event"], []).append(t)
    events = list(by_event)
    if len(events) < 2:
        return (float("nan"), float("nan"))
    gaps = []
    for _ in range(n_boot):
        pool = []
        for _ in range(len(events)):
            pool.extend(by_event[events[rng.randrange(len(events))]])
        a, b = _roi(pool, "s2_realized"), _roi(pool, "live_realized")
        if a is not None and b is not None:
            gaps.append(a - b)
    if len(gaps) < 2:
        return (float("nan"), float("nan"))
    gaps.sort()
    return (gaps[int(0.025 * len(gaps))], gaps[int(0.975 * len(gaps)) - 1])


def run(conn, since: str = REGISTERED, n_forward: int = N_FORWARD) -> dict:
    """PR-7 step 1's scoreboard. Returns PENDING until the registered count is reached."""
    loaded = load_trades(conn, since)
    trades = loaded["trades"]
    n = len(trades)
    roi_live, roi_s2 = _roi(trades, "live_realized"), _roi(trades, "s2_realized")
    gap = None if (roi_live is None or roi_s2 is None) else roi_s2 - roi_live
    fired = [t for t in trades if t["s2_fired"]]

    if n < n_forward:
        verdict = (f"PENDING — {n}/{n_forward} forward hybrid trades. PR-7 step 1 is "
                   f"judged only at {n_forward}; the ROI figures here are a progress "
                   f"readout and must not be quoted as a result.")
        ci = None
    else:
        ci = _boot_ci_gap(trades, random.Random(SEED))
        crosses = not (ci[0] > 0 or ci[1] < 0)
        passed = gap is not None and gap >= MIN_ROI_GAP and not crosses
        verdict = ("PASS — S2 clears the registered bar; it may go live."
                   if passed else
                   "FAIL — the registered bar is not met. Per PR-7 step 1 the "
                   "price-exit family is closed: S2 does not go live, and no variant "
                   "(other theta, trailing) may be tried on this data.")
    return {
        "registered_since": since, "n_trades": n, "n_required": n_forward,
        "n_still_open": loaded["n_open"], "unscorable": loaded["unscorable"],
        "n_s2_fired": len(fired),
        "staked_usd": round(sum(t["staked"] for t in trades), 4),
        "roi_live": None if roi_live is None else round(roi_live, 5),
        "roi_s2": None if roi_s2 is None else round(roi_s2, 5),
        "roi_gap": None if gap is None else round(gap, 5),
        "ci95_gap": None if ci is None else [round(ci[0], 5), round(ci[1], 5)],
        "min_gap_required": MIN_ROI_GAP,
        "verdict": verdict,
        "trades": trades,
        "note": "PR-7 step 1, K=3, SHADOW. S2 = rule 1 tightened to hold_edge <= 0; it "
                "is not executed and does not affect the live book. Both arms are priced "
                "by ops.exits.exit_realized / the ledger's realized_usd.",
    }


def main():
    import argparse

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=REGISTERED,
                    help="PR-7 registered 2026-08-05. Moving it re-opens the registration.")
    ap.add_argument("--trades", action="store_true", help="include the per-trade rows")
    a = ap.parse_args()
    conn = init_db(load_settings().db_path)
    out = run(conn, since=a.since)
    if not a.trades:
        out.pop("trades")
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
