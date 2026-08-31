"""research/shadow_claims.py — PR-1 (#118): score the claims recency candidate forward.

`docs/PREREGISTER.md` registered ONE hypothesis on 2026-07-31 and this module is the only
thing allowed to grade it. It computes nothing that the registration did not name, and it
refuses to print a verdict before the registered sample size is reached.

The registration, verbatim
--------------------------
    改动   `model/claims.py` recency weights (0.0, 0.0, 0.3, 0.7) + seasonal_years=10
    判据   前向 8 个已结算周, paired Brier(model) < Brier(market). 做不到即证伪, 退回默认
    K      1

Two things about that text are worth stating plainly rather than quietly interpreting:

1. **The bar is the MARKET, not the incumbent parameters.** The discovery evidence
   (0.1497 vs 0.1600 over 49 events) was candidate-vs-default, but what was registered is
   candidate-vs-market. That is the harder and the more useful bar — a parameter set that
   beats the default while still losing to the book earns nothing, and `strategy/skill.py`
   already blocks model-path bets on exactly that ratio. Candidate-vs-default is reported
   below as a SECONDARY readout because it is the quantity the discovery was made on, but
   it does not grade anything. Reporting it is not the same as being allowed to pass on it.

2. **`seasonal_years=10` is already the default** and has been since the file was written
   (`model/claims.DEFAULT_PARAMS`). So the registered change moves exactly ONE knob,
   `level_weights`. The registration text lists two and reads like two; it is one.

Why this is a replay and not a live logger
------------------------------------------
`research/shadow_s2.py` needs a live writer because an exit depends on book state that
cannot be reconstructed without choosing, after the fact, which cycle's quotes to use —
and choosing that after settlement is a researcher degree of freedom. Brier has no such
freedom: `asof` is a deterministic function of the event's close time and its release
time, and both are stored. So both arms are replayed here, and the thing that would
otherwise be a silent hazard is instead pinned:

* **Both arms go through `backtest.replay_series`,** which is where the `asof` rules live
  (step back behind the print when the book closed after it; drop an event whose
  `data_horizon` reached past the release). `param_wf.score_matrix` uses a flat close−1h
  and would have given the candidate a different `asof` than the market it is judged
  against — a paired test between two different asofs is not a paired test.
* **The event sets are INTERSECTED and any asymmetric drop is reported,** not absorbed.
  A candidate that fails to predict on some week must not be scored on an easier subset
  than the market; `n_dropped_asymmetric` is in the output for that reason.

What a code change to `model/claims.py` does to this
----------------------------------------------------
Both arms re-run today's code, so a model change moves them together and the pairing
survives — but the effect size does not, and a registration is about a fixed comparison.
`code_fingerprint` is the sha1 of `model/claims.py`; if it differs from the value recorded
at registration, that is in the output and the run says so. It does not silently continue
as if nothing happened, and it does not silently refuse either — which of the two the
right answer is depends on what changed, and that is a judgement for a human.
"""
from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timezone

SERIES = "KXJOBLESSCLAIMS"

# The registration. None of these may be edited to make a result pass — that is the whole
# point of the file they came from.
REGISTERED = "2026-07-31T00:00:00+00:00"   # forward count starts at this settlement
CANDIDATE = {"level_weights": (0.0, 0.0, 0.3, 0.7), "seasonal_years": 10}
N_FORWARD = 8                              # registered sample size
OFFSET = "-1h"                             # the offset the registration's evidence used
K = 1                                      # single pre-registered hypothesis

# The docstring above promised that a fingerprint differing from the registration's would
# be visible in the output. Until 2026-08-27 only the CURRENT fingerprint was emitted, so
# there was nothing to differ FROM and the promise was not kept — the file had in fact
# already changed once, on 2026-08-10, and no run said so. Both changes are recorded here
# with the reason each is or is not numerically inert, because the useful question is
# never "did the bytes move" but "did the two arms move".
REGISTERED_FINGERPRINT = "c23dc975945f"    # model/claims.py at 224ad2e, 2026-07-31
KNOWN_FINGERPRINTS = {
    "54658a3a9771": (
        "2026-08-31 (PR-31) — the ERCOT covariate gate: mu gains w*ercot_cov.mu_shift\n"
        "behind params['ercot_w'], default 0.0. INERT for both arms: neither the\n"
        "registered candidate nor the baseline passes ercot_w, so every number on\n"
        "both sides is bit-identical to the previous fingerprint."),
    "c23dc975945f": "2026-07-31 — the registration itself",
    "6b52d629c385": (
        "2026-08-10 (#118) — DEFAULT_PARAMS made actually readable; seasonal_years, "
        "seasonal_clip, vol_window and sigma_floor had been declared and hardcoded. "
        "INERT for both arms: the hardcoded values WERE the defaults (10 / 0.25 / 27 "
        "levels / 0.02) and the candidate passes seasonal_years=10, so every number on "
        "both sides is unchanged."),
    "8da8764eaa2c": (
        "2026-08-27 (#197, claims/0.2.0, PR-11) — the ISO-week seasonal centre became an "
        "outlier-screened mean so that March-April 2020 stops entering it. NOT inert in "
        "general (it moves 5 of 45 scored events by up to 6.0 nats), but inert for THIS "
        "registration: the screen fires only on ISO weeks 12/13/14/15/18 and PR-1's "
        "counted weeks are ISO 32/33/34. Verified by re-running this scorer across the "
        "change — all three events' cand/default/market Brier are bit-identical."),
}


def code_fingerprint() -> str:
    """sha1 prefix of `model/claims.py`. Recorded on every run so that a mid-flight change
    to the model is visible in the output rather than invisible — both arms would move
    together and the pairing would survive, but the registered effect size would not."""
    p = pathlib.Path(__file__).resolve().parent.parent / "model" / "claims.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def code_change_note(fp: str | None = None) -> dict:
    """What the current fingerprint means for the registration. An UNKNOWN fingerprint is
    the loud case: it means `model/claims.py` changed and nobody wrote down whether the
    registered comparison survived it, which is the state this function exists to end."""
    fp = fp or code_fingerprint()
    known = fp in KNOWN_FINGERPRINTS
    return {"fingerprint": fp, "registered_fingerprint": REGISTERED_FINGERPRINT,
            "code_changed_since_registration": fp != REGISTERED_FINGERPRINT,
            "change_is_documented": known,
            "note": KNOWN_FINGERPRINTS.get(fp, (
                "UNDOCUMENTED CHANGE — model/claims.py no longer matches any recorded "
                "version. Establish whether the registered comparison survived it and "
                "record the answer in KNOWN_FINGERPRINTS before reading the numbers "
                "below as PR-1's."))}


def _close_times(conn, series: str) -> dict[str, datetime]:
    """{calendar key -> event close} for every settled period of the series."""
    from prediction_market_macro.util.periods import kalshi_period_to_key
    out: dict[str, datetime] = {}
    for r in conn.execute(
            "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
            " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
            " AND s.result IN ('yes','no') GROUP BY s.period", (series,)).fetchall():
        key = kalshi_period_to_key(r["period"])
        if key and r["ct"]:
            out[key] = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
    return out


def _arm(conn, params: dict | None) -> dict[str, dict]:
    """{period -> {model, market, n_legs}} for one parameter set, at OFFSET."""
    from prediction_market_macro.research.backtest import replay_series
    rep = replay_series(conn, SERIES, asof_offsets=(OFFSET,), params=params)
    out = {}
    for p in rep["per_release"]:
        m, k = p.get(f"brier_model{OFFSET}"), p.get(f"brier_market{OFFSET}")
        if m is None or k is None:
            continue
        out[p["period"]] = {"model": float(m), "market": float(k),
                            "n_legs": p.get(f"n_legs{OFFSET}")}
    return out


def _wilcoxon(diffs: list[float]) -> tuple[float | None, float | None]:
    """(statistic, one-sided p) for H1: diffs < 0. None when scipy cannot decide.

    One-sided because the registration is one-sided: the candidate has to BEAT the market,
    and a candidate that loses significantly is failed by the same clause that a candidate
    which merely ties is.
    """
    nz = [d for d in diffs if d != 0.0]
    if len(nz) < 5:
        return None, None
    from scipy.stats import wilcoxon
    r = wilcoxon(nz, alternative="less")
    return float(r.statistic), float(r.pvalue)


def run(conn, asof: datetime | None = None) -> dict:
    """The PR-1 readout. `PENDING` until `N_FORWARD` post-registration weeks have settled.

    Below the registered n there is no verdict and none is implied: the ROI-style numbers
    in the output are a progress readout. #128 is what reporting an early number as though
    it meant something looks like afterwards.
    """
    now = asof or datetime.now(timezone.utc)
    reg = datetime.fromisoformat(REGISTERED)
    closes = _close_times(conn, SERIES)

    default, cand = _arm(conn, None), _arm(conn, CANDIDATE)
    fwd = {p for p, t in closes.items() if reg <= t <= now}
    both = sorted(fwd & default.keys() & cand.keys(), key=lambda p: closes[p])
    asym = sorted((fwd & (default.keys() | cand.keys())) - set(both))

    rows = [{"period": p, "close": closes[p].isoformat(),
             "cand": round(cand[p]["model"], 6),
             "default": round(default[p]["model"], 6),
             "market": round(default[p]["market"], 6),
             "n_legs": default[p]["n_legs"]} for p in both]
    # market Brier does not depend on params, but it DOES depend on asof, and asof is
    # params-independent — so the two arms' market columns must agree exactly. If they
    # ever do not, something is asof-dependent that should not be, and that is worth a
    # loud row rather than a quiet average.
    mismatched = [p for p in both
                  if abs(default[p]["market"] - cand[p]["market"]) > 1e-9]

    out = {
        "registered": REGISTERED, "k": K, "series": SERIES,
        "candidate": {k: list(v) if isinstance(v, tuple) else v
                      for k, v in CANDIDATE.items()},
        "offset": OFFSET, "asof": now.isoformat(),
        "code_fingerprint": code_fingerprint(),
        "code_change": code_change_note(),
        "n_forward": len(both), "n_required": N_FORWARD,
        "n_dropped_asymmetric": len(asym), "dropped": asym,
        "market_column_mismatch": mismatched,
        "events": rows,
    }
    if rows:
        prim = [r["cand"] - r["market"] for r in rows]
        sec = [r["cand"] - r["default"] for r in rows]
        stat, p = _wilcoxon(prim)
        out["primary"] = {
            "comparison": "candidate vs market (REGISTERED)",
            "mean_cand": round(sum(r["cand"] for r in rows) / len(rows), 6),
            "mean_market": round(sum(r["market"] for r in rows) / len(rows), 6),
            "mean_diff": round(sum(prim) / len(prim), 6),
            "n_better": sum(1 for d in prim if d < 0),
            "wilcoxon_stat": stat, "wilcoxon_p_one_sided": p,
        }
        out["secondary"] = {
            "comparison": "candidate vs default params (DISCOVERY quantity — grades "
                          "nothing; the registered bar is the market)",
            "mean_default": round(sum(r["default"] for r in rows) / len(rows), 6),
            "mean_diff": round(sum(sec) / len(sec), 6),
            "n_better": sum(1 for d in sec if d < 0),
        }
    out["criterion"] = (
        f"PR-1, K={K}: {N_FORWARD} forward settled weeks from {REGISTERED[:10]}, paired "
        "Brier(candidate) < Brier(market) at the -1h offset. Failing it falsifies the "
        "recency weights and the defaults stand. No re-search on failure — the "
        "registration says 'do not search further' and that clause is the reason this "
        "is a K=1 test and not a K=13 one.")
    out["verdict"] = _verdict(out)
    return out


def _verdict(out: dict) -> str:
    if out["n_forward"] < out["n_required"]:
        return (f"PENDING — {out['n_forward']}/{out['n_required']} forward weeks. "
                "No verdict, and the numbers above are a progress readout, not a result.")
    pr = out.get("primary") or {}
    p = pr.get("wilcoxon_p_one_sided")
    if pr.get("mean_diff") is not None and pr["mean_diff"] < 0 and p is not None and p < 0.05:
        return (f"PASSED — candidate beat the market by {-pr['mean_diff']:.5f} Brier/week "
                f"on {pr['n_better']}/{out['n_forward']} weeks (one-sided p={p:.4f}).")
    return (f"FALSIFIED — the registered bar was Brier(candidate) < Brier(market) over "
            f"{out['n_required']} forward weeks; observed mean difference "
            f"{pr.get('mean_diff')}, one-sided p={p}. Defaults stand, and per the "
            "registration there is no re-search.")


def main():
    import argparse
    import json

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    conn = init_db(a.db or load_settings().db_path)
    print(json.dumps(run(conn), ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
