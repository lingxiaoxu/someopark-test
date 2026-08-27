"""research/branch_parity.py — refuse to grade a model production does not run (#196).

Every metric this project trusts — the §9.5 gate row, the calibration table, the (δ,λ)
fits, `param_select`'s DSR choice — is computed by replaying settled history through
`predict()`. That is only evidence about production if the replay takes the SAME code
path production takes. Twice now it did not:

    KXFED       history: rule+dgs2 36/40, ff 1/40   live: ff on 5 of the next 6 meetings
    KXAAAGASW   history: damped_trend_fallback 51/73  live: aaa_daily_anchor 4/4

Both for the same reason — a main input has no usable history (ZQ expiries 404 on
yfinance; AAA_DAILY starts 2026-07-31) — so the replay silently degrades to a fallback
and the resulting row describes a model that has never placed a bet. KXAAAGASW is the
worst model-vs-market Brier in the gate table (0.115 vs 0.014) and that number scores the
fallback, which means even the standing "the model never beats the market" conclusion is
partly measuring the wrong thing. Neither case announced itself; both were found by hand.
This module is the announcement.

**How a branch is identified.** From the model's own `Pred.inputs`, never from anything
this module knows about a particular model:

  1. `inputs["mode"]` when present — the model labelled its own branch. This is the
     contract. A model with a branch worth knowing about should emit `mode`.
  2. otherwise the SET OF INPUT KEY NAMES (values ignored). A fallback path almost always
     omits the inputs it could not read, so the key set moves with the branch for free.

Rule 2 is a weak observer on purpose: it sees a branch that changes which inputs exist,
and it is blind to one that only changes their values (claims' `seasonal = 0.0` when
fewer than 3 same-week history points exist is invisible here). The fix for a blind spot
is to make the model emit `mode`, NOT to teach this module about that model — a per-series
table of branch rules would go stale exactly when the model changes, i.e. when it matters.

**What counts as parity.** Compare the branch mix of the historical sample against the
branch mix production is actually running, and require that the branch production runs
most of the time also accounts for a majority of the sample it is being graded on. Not
"identical mixes": a horizon-dependent branch (KXFED prices near meetings off ZQ and far
ones off the rule) legitimately mixes, and demanding equality would fire forever.

READ-ONLY with respect to the store: this module predicts and counts, it never writes.

    conda run -n someopark_run python -m prediction_market_macro.research.branch_parity
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone

_PARAM_TAIL = re.compile(r"\([^()]*\)\s*$")

# A live branch must cover at least this much of the graded sample for the grade to be
# about the live model. 0.5 is the weakest defensible reading of "this row describes
# production": anything below it and the majority of the evidence is about something else.
# It is a threshold on the HISTORICAL share of the LIVE branch, not on the mixes' distance
# — a sample can be 100% one branch and still be fine if that is the branch running.
MIN_HIST_SHARE = 0.50

# diagnostic keys that ride along on every branch and would otherwise split the signature
_IGNORED_SUFFIXES = ("_retired",)


def branch_of(inputs: dict | None) -> str:
    """Canonical branch label for one prediction. See the module docstring for the rule."""
    if not inputs:
        return "unknown:no_inputs"
    mode = inputs.get("mode")
    if isinstance(mode, str) and mode:
        # A trailing parenthesis carries DATA, not identity: energy.py labels its middle
        # branch `drift_regression(n=17)`, so KXAAAGASW's 18 events on that path arrive as
        # 18 distinct one-event "branches" and every share in the mix collapses to 1/73.
        # Stripping it recovers the true reading — damped_trend_fallback 51, drift 18,
        # aaa_daily_anchor 4 — which is the split that was found by hand in #188/#194.
        # Read-side normalisation rather than a model edit: the models are the production
        # path, and a label convention is not worth a version bump on four of them.
        return _PARAM_TAIL.sub("", mode).strip()
    keys = sorted(k for k in inputs
                  if not any(k.endswith(s) for s in _IGNORED_SUFFIXES))
    return "keys:" + ",".join(keys)


def mix_from_counts(counts: dict | Counter | None) -> dict:
    """{branch: n} → the mix bundle `parity_check` consumes. Public because
    `replay_series` already produces the historical counts as a by-product of the replay
    the gate runs anyway, and re-deriving them would mean predicting every settled event
    a second time for an answer that is already in hand."""
    c = Counter(counts or {})
    n = sum(c.values())
    return {"n": n, "counts": dict(c.most_common()),
            "shares": {k: round(v / n, 4) for k, v in c.most_common()} if n else {}}


def hist_branch_mix(conn, series: str, max_events: int = 200,
                    params: dict | None = None) -> dict:
    """Branch mix of the sample the gate/calibration is computed on.

    Deliberately routed through `replay_series` rather than re-implementing the event
    walk. The asof rule there is subtle (close−1h, clamped behind the settling print,
    events whose `data_horizon` reached past the print dropped) and a second copy of it
    would drift from the one being graded — at which point this check would be measuring
    its own walk instead of the gate's.
    """
    from prediction_market_macro.research.backtest import replay_series
    rep = replay_series(conn, series, asof_offsets=("-1h",), max_events=max_events,
                        params=params)
    return {**mix_from_counts(rep["agg"].get("branch_mix-1h")),
            "n_events": rep["agg"]["n"], "skipped": rep["agg"]["skipped"]}


def tradeable_periods(conn, series: str, now: datetime) -> tuple[list[str], str]:
    """(periods, window) — the open periods production could take a position in.

    NOT simply every open period. `decide` refuses an entry outside
    0.03 <= days_to_close <= GATES["max_days_to_close"] (7), and the decision replay
    scans exactly that window, so a far-dated period is predicted daily and never bet on.
    Counting those manufactures failures: the CPI family prices months the Cleveland
    nowcast does not reach yet, which reads as a branch divergence and is really a period
    nobody will trade for another two months. The gate protects money, so the live side
    must be the periods money can reach.

    When nothing is in the window — the normal state of a monthly series for three weeks
    out of four — fall back to the single nearest close. It is a proxy, not the real
    thing: the branch is read at today's asof rather than at the asof production will
    decide on, and an input that arrives in the meantime (the nowcast for the next ref
    month is the live example) will flip it. Returning nothing instead would be worse —
    it makes the monthly series unverifiable, and an unverifiable series is treated as a
    failure here, so the gate would be closed by the calendar rather than by evidence.
    The window is reported so a caller can tell the two apart.
    """
    from prediction_market_macro.strategy.decision import GATES
    from prediction_market_macro.util.periods import kalshi_period_to_key
    lead = float(GATES.get("max_days_to_close", 7.0))
    rows = conn.execute(
        "SELECT period, MAX(close_time) ct FROM contracts WHERE series=?"
        " AND status='active' GROUP BY period", (series,)).fetchall()
    live = []
    for r in rows:
        key = kalshi_period_to_key(r["period"])
        if not key or not r["ct"]:
            continue
        ct = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
        dtc = (ct - now).total_seconds() / 86400.0
        if dtc >= 0.03:
            live.append((dtc, key))
    inside = [k for d, k in live if d <= lead]
    if inside:
        return inside, "entry_window"
    if live:
        return [min(live)[1]], "nearest_open"
    return [], "none_open"


def recorded_branch_mix(conn, series: str, now: datetime) -> dict:
    """What production ACTUALLY ran, read out of `preds`, restricted to rows written
    while their period was inside the entry window.

    This is the strongest live evidence available and it is free: `predict_all` writes a
    row per open period every morning, so every day a period spent tradeable is on record.
    Filtered to the CURRENT production `model_version` — `preds` also holds the shadow
    members (ensemble/*, bridge/*, chronos2/*) and yesterday's model after a bump, and
    counting either would answer a question nobody asked. That filter is also why this
    can come back empty (a fresh bump, or a monthly series whose last close predates the
    ~75-day candle/pred retention), which is what the fallbacks in `live_branch_mix` are
    for.

    Rows with no `inputs_json` are dropped rather than counted as a branch: an unwritten
    record is an absence of evidence, and letting it become its own "branch" would dilute
    every share in the mix with the store's own gaps.
    """
    import importlib
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    from prediction_market_macro.strategy.decision import GATES
    from prediction_market_macro.util.periods import kalshi_period_to_key
    disp = SERIES_DISPATCH.get(series)
    if disp is None:
        return {**mix_from_counts(None), "source": "recorded", "n_no_inputs": 0}
    version = getattr(importlib.import_module(disp[0]), "VERSION", None)
    lead = float(GATES.get("max_days_to_close", 7.0))
    closes = {}
    for r in conn.execute("SELECT period, MAX(close_time) c FROM contracts WHERE"
                          " series=? GROUP BY period", (series,)):
        k = kalshi_period_to_key(r["period"])
        if k and r["c"]:
            closes[k] = datetime.fromisoformat(r["c"].replace("Z", "+00:00"))
    c, blank = Counter(), 0
    for r in conn.execute("SELECT period, asof, inputs_json FROM preds WHERE series=?"
                          " AND model_version=?", (series, version)):
        close = closes.get(r["period"])
        if close is None:
            continue
        dtc = (close - datetime.fromisoformat(r["asof"])).total_seconds() / 86400.0
        if not 0.03 <= dtc <= lead:
            continue
        inputs = json.loads(r["inputs_json"] or "null")
        if not inputs:
            blank += 1
            continue
        c[branch_of(inputs)] += 1
    return {**mix_from_counts(c), "source": "recorded", "model_version": version,
            "n_no_inputs": blank}


def live_branch_mix(conn, series: str, now: datetime | None = None,
                    params: dict | None = None, use_params: bool = True) -> dict:
    """The branch production runs when it can actually bet, by the best available means.

    Three sources, in descending order of how much they are worth:

      recorded    what production wrote to `preds` while the period was inside the entry
                  window. A fact, not a reconstruction. Empty after a version bump or for
                  a series with no recent tradeable close.
      entry_window  predict today's in-window periods at `now`. A reconstruction, but of
                  a decision production is about to make for real.
      nearest_open  predict the single nearest open period at `now`. A PROXY, and the one
                  place this check can be wrong: the branch is read at today's asof rather
                  than at the asof production will decide on, so an input that lands in
                  between flips it. Measured 2026-08-27 on KXPCECORE — the proxy said
                  `bridge_on_predicted_cpi` (August CPI has not printed yet) while the
                  recorded in-window mix said `bridge_on_actual_cpi` 41/41, because by the
                  time August PCE is tradeable the August CPI will have printed. That is
                  exactly why `recorded` is tried first and why the source is reported.
    """
    import importlib
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    now = now or datetime.now(timezone.utc)
    disp = SERIES_DISPATCH.get(series)
    if disp is None:
        return {**mix_from_counts(None), "error": "series not dispatchable"}
    rec = recorded_branch_mix(conn, series, now)
    if rec["n"]:
        rec["asof"] = now.isoformat()
        return rec
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    if use_params and params is None:
        from prediction_market_macro.research import param_select
        params = param_select.current(conn, series) or None
    keys, window = tradeable_periods(conn, series, now)
    c, err = Counter(), 0
    for key in keys:
        try:
            pred = fn(conn, now, key, series=series, params=params)
        except Exception:                                          # noqa: BLE001
            err += 1
            continue
        c[branch_of(pred.inputs)] += 1
    out = mix_from_counts(c)
    out["n_failed"] = err
    out["source"] = window
    out["periods"] = keys
    out["asof"] = now.isoformat()
    return out


def parity_check(hist: dict, live: dict) -> dict:
    """Pure verdict from two mixes (unit-testable, no DB).

    `parity=False` on an empty live sample is intentional and is not a bug report about
    the model: with no open period there is nothing to verify the grade against, and this
    feeds a real-money gate. A series with no open markets cannot trade anyway, so the
    conservative answer costs nothing.
    """
    hs, ls = hist.get("shares") or {}, live.get("shares") or {}
    if not ls:
        return {"parity": False, "unknown": True, "n_hist": hist.get("n", 0), "n_live": 0,
                "reason": "no live prediction to compare against"}
    if not hs:
        return {"parity": False, "unknown": True, "n_hist": 0,
                "n_live": live.get("n", 0),
                "reason": "no historical sample — nothing was graded"}
    live_branch, live_share = max(ls.items(), key=lambda kv: kv[1])
    hist_branch, hist_share = max(hs.items(), key=lambda kv: kv[1])
    covered = float(hs.get(live_branch, 0.0))
    tvd = round(0.5 * sum(abs(hs.get(k, 0.0) - ls.get(k, 0.0))
                          for k in set(hs) | set(ls)), 4)
    ok = covered >= MIN_HIST_SHARE
    n_h = hist.get("n", 0)
    reason = None if ok else (
        f"graded on {hist_branch} {int(round(hist_share * n_h))}/{n_h}"
        f" but production runs {live_branch} ({live_share:.0%} of {live.get('n')} {live.get('source')} obs);"
        f" the live branch is only {covered:.0%} of the graded sample")
    return {"parity": ok, "unknown": False, "live_source": live.get("source"),
            "live_branch": live_branch,
            "live_share": round(live_share, 4), "hist_branch": hist_branch,
            "hist_share": round(hist_share, 4),
            "hist_share_of_live_branch": round(covered, 4), "tvd": tvd,
            "n_hist": n_h, "n_live": live.get("n", 0), "reason": reason}


def check(conn, series: str, now: datetime | None = None,
          max_events: int = 200, params: dict | None = None) -> dict:
    """hist + live + verdict for one series."""
    h = hist_branch_mix(conn, series, max_events=max_events, params=params)
    l = live_branch_mix(conn, series, now=now, params=params)
    v = parity_check(h, l)
    return {"series": series, **v, "hist": h, "live": l}


def check_all(conn, now: datetime | None = None, log=None) -> dict:
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    out = {}
    for s in SERIES_DISPATCH:
        try:
            out[s] = check(conn, s, now=now)
        except Exception as e:                                     # noqa: BLE001
            out[s] = {"series": s, "parity": False, "unknown": True,
                      "reason": f"{type(e).__name__}: {e}"}
        if log:
            r = out[s]
            log(f"{s:<16} {'OK ' if r.get('parity') else 'FAIL'}  "
                f"{(r.get('reason') or 'live branch dominates the graded sample')[:120]}")
    return out


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--series")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    conn = init_db(load_settings(require_keys=False).db_path)
    res = ({a.series: check(conn, a.series)} if a.series
           else check_all(conn, log=None if a.json else print))
    if a.json:
        print(json.dumps(res, indent=1))
        return
    bad = [s for s, r in res.items() if not r.get("parity")]
    print(f"\n{len(res) - len(bad)}/{len(res)} series graded on the branch they run")
    for s in bad:
        r = res[s]
        print(f"\n{s}:  {r.get('reason')}")
        print(f"   hist {json.dumps((r.get('hist') or {}).get('counts', {}))}")
        print(f"   live {json.dumps((r.get('live') or {}).get('counts', {}))}")


if __name__ == "__main__":
    main()
