"""research/shadow_nowcast.py — PR-8 and PR-10: grade the Cleveland nowcast anchors (#195).

Two registrations in `docs/PREREGISTER.md` named this file as their scorer and neither one
got it. PR-8 was registered 2026-08-15, PR-10 on 2026-08-27, and PR-11's own registration
already records the debt in writing ("PR-8/PR-10 的评分器登记后拖了两周还没写, 那个债不再加
一笔"). A criterion nothing computes is not a criterion, so this module exists to make both
of them computable — and it deliberately grades them in the shape they were registered in,
including the parts that make them harder to pass.

The two registrations
---------------------
    PR-8  (2026-08-15, K=1)   model/cpi.py YoY branch: yoy_mu <- Cleveland YoY nowcast.
          判据: 前向 6 个已结算 CPI-YoY 族事件, T-26h 成对逐腿 Brier, candidate <
                当前生产模型(含每日 argmin 参数), 且 6 事件中 >=4 个方向一致.

    PR-10 (2026-08-27, K=12)  model/cpi.py predict_mom(core=False): mu <- Cleveland MoM
          判据: 前向 6 个已结算 KXCPI 事件, T-26h 成对区间对数似然, candidate 的均值 >
                生产模型, 且 6 个里 >=4 个逐事件为正. 不换混合权重 w 重试, 不换度量重试.

Both anchors are ALREADY WIRED (cpi/0.3.0 then 0.4.0), so "candidate" is what production
runs today and the arm that has to be reconstructed is the un-anchored one. That is done by
passing `nowcast_anchor=False` to today's code — a key added for exactly this and proved a
no-op at its default (200/200 settled CPI-family predictions bit-identical to HEAD; see
`model/cpi.py`). It is not a new branch: production already falls back to the internal chain
whenever the feed is stale, so the baseline arm is a path production takes for real.

Three things in those two registrations decide the whole design, and each of them makes the
test harder rather than easier.

**PR-8's count is HEADLINE, six events — not the family, three.** The 判据 row says "两系列
合计" but the 改道注记 written hours later re-scoped it ("若前向 6 事件 headline 反向...")
and the 结论 row records the count that way ("headline 0 / 6"). Both series settle on the
same CPI print, so family-of-six is three months and headline-of-six is six. The primary
count is the slower one. The family reading is still computed and reported, labelled as the
pre-改道 reading, because pretending the 判据 row does not say what it says would be its own
kind of dishonesty — but `verdict()` reads the headline count.

**Core is graded on its own, and its adoption was never evidence-based.** KXCPICOREYOY was a
measured TIE (Δ−0.0005 Brier, 19/44) and the registration records in full that it was wired
by user decision on 2026-08-15, not by evidence, with its own re-open rule: "前向确认窗若对
core 反向, 先复议这一半". So core gets its own count and its own verdict line. Folding it
into the headline count would let the half with no evidence behind it borrow the half with
evidence, in either direction.

**T-26h is what was registered, so T-26h is the primary asof.** PR-10's discovery table
underneath the registration used close−1h, which is a real discrepancy inside one entry.
The 判据 row binds, and it is also the honest choice on the merits: `ops/decide_all.py`
forces PASS on any prediction older than 26h, so close−26h is the EARLIEST asof at which a
prediction could still be acted on at the close — the hardest asof the decision layer would
ever accept, not the most flattering. The discovery's close−1h is computed too and reported
as `secondary`, marked as grading nothing, so that a later reader can see the discrepancy
rather than have to find it.

What is deliberately NOT here
----------------------------
No mixing weight. PR-10 registered "不换混合权重 w 重试" and PR-8 "不换混合权重重试", so
this file scores exactly two arms — anchored and not — and there is nowhere to put a w.
No Brier for PR-10 and no interval LL for PR-8: each registration names ONE metric, and
computing the other one here would be the retry both of them forbid, dressed as a readout.

The baseline arm carries the SAME adopted params as production, read PIT at each event's own
asof (#198). An arm that differed in the anchor AND in `w_last` is not a paired test of the
anchor. The registration's own words are "含每日 argmin 参数".
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
from datetime import datetime, timezone

INF = float("inf")

# ── the registrations. None of these may be edited to make a result pass. ────────────
REGISTERED_PR8 = "2026-08-15T00:00:00+00:00"
REGISTERED_PR10 = "2026-08-27T00:00:00+00:00"
PR8_PRIMARY = "KXCPIYOY"            # the 改道注记's count, and the 结论 row's
PR8_CORE = "KXCPICOREYOY"           # wired by user decision, graded separately
PR10_SERIES = "KXCPI"
N_FORWARD = 6                       # both registrations
MIN_POSITIVE = 4                    # both registrations, out of N_FORWARD
OFFSET_HOURS = 26                   # "T-26h", both registrations
DISCOVERY_HOURS = 1                 # PR-10's evidence table's asof; grades NOTHING
K_PR8 = 1
K_PR10 = 12

BASE_ARM = {"nowcast_anchor": False}    # the internal chain, i.e. cpi/0.2.0's mu
CAND_ARM: dict = {}                     # production's default: anchored

# `shadow_width`'s floor, same reasoning: an unbounded penalty is not a measurement. One
# print landing off the ladder would otherwise decide a six-event mean by itself, in
# whichever direction the narrower arm happened to fall.
_P_FLOOR = 0.5 / 20_000.0

# Stamped by `--stamp`; see `code_change_note`. A mid-flight edit to model/cpi.py moves both
# arms together, so the pairing survives but the registered effect size does not, and that
# has to be visible in the report rather than inferred from git.
REGISTERED_FINGERPRINT = "a1f654f0d8e9"
KNOWN_FINGERPRINTS: dict[str, str] = {
    "a1f654f0d8e9": "cpi/0.4.0 as this scorer was written, plus the `nowcast_anchor` key "
                    "that makes the un-anchored arm reachable. Proved a no-op at its "
                    "default on the live db: 200/200 settled CPI-family predictions "
                    "bit-identical to HEAD (comps + inputs + data_horizon), and 151/151 "
                    "moved when flipped. VERSION deliberately not bumped.",
}


def code_fingerprint() -> str:
    p = pathlib.Path(__file__).resolve().parent.parent / "model" / "cpi.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def code_change_note(fp: str | None = None) -> dict:
    """Same contract as `shadow_width.code_change_note`, deliberately — two scorers that
    report the same fact under two different key names is how a reader stops reading it."""
    fp = fp or code_fingerprint()
    if REGISTERED_FINGERPRINT == "PENDING-STAMP":
        return {"fingerprint": fp, "registered_fingerprint": None,
                "code_changed_since_registration": None, "change_is_documented": False,
                "note": "registration fingerprint not stamped yet — run --stamp once, at "
                        "the commit that lands this scorer, and never again."}
    return {"fingerprint": fp, "registered_fingerprint": REGISTERED_FINGERPRINT,
            "code_changed_since_registration": fp != REGISTERED_FINGERPRINT,
            "change_is_documented": fp in KNOWN_FINGERPRINTS,
            "note": KNOWN_FINGERPRINTS.get(fp, (
                "UNDOCUMENTED CHANGE — model/cpi.py no longer matches any recorded "
                "version. Both arms move together so the pairing survives, but the "
                "registered effect size may no longer describe what is being scored. "
                "Establish whether PR-8's and PR-10's comparisons survived it and record "
                "the answer in KNOWN_FINGERPRINTS before reading the numbers below as "
                "theirs."))}


def _adopted(conn, series: str, asof: datetime) -> dict:
    """The params production ran at `asof`, PIT (#198). Empty dict when nothing is adopted.

    Both arms get these. `manual_params` is PIT-gated on `created_ts`, so an override
    adopted after the event does not reach back into it.
    """
    try:
        from prediction_market_macro.research.param_select import manual_params
    except Exception:                                            # noqa: BLE001
        return {}
    try:
        got = manual_params(conn, series, asof)
    except Exception:                                            # noqa: BLE001
        return {}
    return dict(got[0]) if got and got[0] else {}


def _arms(conn, series: str, key: str, asof: datetime):
    """(base_pred, cand_pred) from the same code at the same asof, differing only in the
    anchor. Raises whatever the model raises — the caller records the event as dropped."""
    import importlib

    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    adopted = _adopted(conn, series, asof)
    base = fn(conn, asof, key, series=series, params={**adopted, **BASE_ARM})
    cand = fn(conn, asof, key, series=series, params={**adopted, **CAND_ARM})
    return base, cand, adopted


def _interval_ll(dist, lo: float, hi: float) -> tuple[float, bool]:
    p = (float(dist.cdf(hi)) if hi != INF else 1.0) - \
        (float(dist.cdf(lo)) if lo != -INF else 0.0)
    if p < _P_FLOOR:
        return math.log(_P_FLOOR), True
    return math.log(p), False


def score_series(conn, series: str, registered: str, metric: str,
                 hours: int = OFFSET_HOURS, asof: datetime | None = None) -> dict:
    """Both arms on every event of `series` that SETTLED after `registered`.

    `metric` is "brier" (PR-8, mean per-leg Brier against the realised leg outcome — no
    quote needed, so no self-selected sample) or "interval_ll" (PR-10, log-probability of
    the interval the settled ladder pins the print into, tails included).
    """
    from prediction_market_macro.research import param_wf, shadow_width
    now = asof or datetime.now(timezone.utc)
    reg = datetime.fromisoformat(registered)
    events, dropped = [], []
    for ev in shadow_width._events(conn, series):
        if ev["close_ts"] <= reg:
            continue                        # settled before registration; not forward
        if ev["close_ts"] > now:
            continue
        a = shadow_width.asof_for(ev, hours=hours)
        try:
            base, cand, adopted = _arms(conn, series, ev["key"], a)
        except Exception as e:                                   # noqa: BLE001
            dropped.append({"period": ev["period"], "why": f"arm raised: {e!r}"})
            continue
        # `replay_series`'s leak guard: an input whose vintage read is not asof-bounded can
        # reach past the print no matter where asof sits.
        if ev["release_ts"] is not None and any(
                p.data_horizon is not None and p.data_horizon >= ev["release_ts"]
                for p in (base, cand)):
            dropped.append({"period": ev["period"],
                            "why": "data_horizon reached past the release"})
            continue
        anchored = "nowcast_date" in cand.inputs
        row = {"period": ev["key"], "close": ev["close_ts"].isoformat(),
               "asof": a.isoformat(), "adopted_params": adopted,
               "anchored": anchored,
               "nowcast_date": cand.inputs.get("nowcast_date")}
        if metric == "brier":
            legs = param_wf.event_legs(conn, series, ev["period"])
            b_base = param_wf.brier(base, series, legs)
            b_cand = param_wf.brier(cand, series, legs)
            if b_base is None or b_cand is None:
                dropped.append({"period": ev["period"], "why": "no scorable legs"})
                continue
            # PR-8's direction: LOWER Brier is better, so the candidate wins when the
            # difference base-minus-candidate is positive. Every downstream test below is
            # written on `delta`, so the sign convention lives in exactly this one place.
            row.update({"n_legs": len(legs), "brier_base": round(b_base, 6),
                        "brier_cand": round(b_cand, 6),
                        "delta": round(b_base - b_cand, 6)})
        else:
            iv = shadow_width.realized_interval(ev["legs"])
            if iv is None:
                dropped.append({"period": ev["period"],
                                "why": "settlement pattern pins no interval"})
                continue
            lo, hi, kind = iv
            ll_b, fl_b = _interval_ll(base.dist, lo, hi)
            ll_c, fl_c = _interval_ll(cand.dist, lo, hi)
            row.update({"interval": [lo, hi], "interval_kind": kind,
                        "ll_base": round(ll_b, 6), "ll_cand": round(ll_c, 6),
                        "floored": bool(fl_b or fl_c),
                        "delta": round(ll_c - ll_b, 6)})
        events.append(row)
    out = {"series": series, "metric": metric, "registered": registered,
           "offset": f"-{hours}h", "n": len(events),
           "n_required": N_FORWARD, "dropped": dropped, "events": events}
    if events:
        d = [e["delta"] for e in events]
        out["mean_delta"] = round(sum(d) / len(d), 6)
        out["n_positive"] = sum(x > 0 for x in d)
        out["n_unanchored"] = sum(not e["anchored"] for e in events)
    return out


# The core half's provenance is a FACT of the registration, not a consequence of failing it.
# It therefore rides on EVERY core verdict, PENDING included: a reader who sees "0/6" for
# core and nothing else will read it as evidence merely being late, when the truth is that
# this half was never carried by evidence in the first place.
CORE_BASIS = ("Core was wired by user decision on 2026-08-15, NOT by evidence — the"
              " historical equivalent was a tie (Δ−0.0005 Brier, 19/44).")


def _verdict(sc: dict, label: str, on_fail: str) -> str:
    n = sc["n"]
    if n < N_FORWARD:
        return (f"PENDING — {n}/{N_FORWARD} forward settled events for {label}. The"
                f" numbers above are a progress readout, not a result.")
    if sc["mean_delta"] > 0 and sc["n_positive"] >= MIN_POSITIVE:
        return (f"PASS — mean delta {sc['mean_delta']:+.5f} over {n} events,"
                f" {sc['n_positive']}/{n} individually positive.")
    return (f"FALSIFIED — mean delta {sc['mean_delta']:+.5f},"
            f" {sc['n_positive']}/{n} positive. {on_fail}")


def run(conn, asof: datetime | None = None) -> dict:
    now = asof or datetime.now(timezone.utc)
    fp = code_fingerprint()
    pr8 = score_series(conn, PR8_PRIMARY, REGISTERED_PR8, "brier", asof=now)
    core = score_series(conn, PR8_CORE, REGISTERED_PR8, "brier", asof=now)
    pr10 = score_series(conn, PR10_SERIES, REGISTERED_PR10, "interval_ll", asof=now)
    pr10_disc = score_series(conn, PR10_SERIES, REGISTERED_PR10, "interval_ll",
                             hours=DISCOVERY_HOURS, asof=now)

    fam_n = pr8["n"] + core["n"]
    fam_d = [e["delta"] for e in pr8["events"]] + [e["delta"] for e in core["events"]]
    out = {
        "asof": now.isoformat(),
        "code": code_change_note(fp),
        "pr8": {
            "k": K_PR8, "registered": REGISTERED_PR8,
            "headline": pr8,
            "core": core,
            "family_reading": {
                "comparison": "the 判据 row's 两系列合计 count, superseded hours later by"
                              " the 改道注记 and by the 结论 row's 'headline 0 / 6'."
                              " Reported so the discrepancy is visible; grades nothing.",
                "n": fam_n,
                "mean_delta": round(sum(fam_d) / len(fam_d), 6) if fam_d else None,
                "n_positive": sum(x > 0 for x in fam_d),
            },
            "criterion": (
                f"PR-8, K={K_PR8}: {N_FORWARD} forward settled {PR8_PRIMARY} events from"
                f" {REGISTERED_PR8[:10]}, paired mean per-leg Brier at -{OFFSET_HOURS}h,"
                f" anchored vs un-anchored, both arms carrying the PIT-adopted argmin"
                f" params. Pass = mean(base) > mean(cand) AND at least"
                f" {MIN_POSITIVE}/{N_FORWARD} individually better. This count was"
                f" DOWNGRADED to confirmatory monitoring on 2026-08-15 — the anchor is"
                f" already wired on the historical equivalent (45 events, Brier 0.0904 ->"
                f" 0.0610), so failing it does not un-wire anything automatically; the"
                f" registration's instruction is 如实记录并复议下线."),
            "verdict": _verdict(
                pr8, PR8_PRIMARY,
                "The registration says this is recorded honestly and the anchor's removal"
                " is then RECONSIDERED (复议下线) — it is not an automatic revert, and it"
                " is not a licence to retry with a mixing weight."),
            "core_verdict": _verdict(
                core, PR8_CORE,
                "The registration's rule for this half is 先复议这一半.") + " " + CORE_BASIS,
        },
        "pr10": {
            "k": K_PR10, "registered": REGISTERED_PR10,
            "primary": pr10,
            "secondary_discovery_asof": {
                **pr10_disc,
                "comparison": f"-{DISCOVERY_HOURS}h, the asof PR-10's own evidence table"
                              " used. The 判据 row says T-26h and that is what grades."
                              " Present so the discrepancy inside the registration is"
                              " visible; this line grades nothing and must not be quoted"
                              " as a result.",
            },
            "criterion": (
                f"PR-10, K={K_PR10}: {N_FORWARD} forward settled {PR10_SERIES} events from"
                f" {REGISTERED_PR10[:10]}, paired interval log-likelihood at"
                f" -{OFFSET_HOURS}h. Pass = mean dLL > 0 AND at least"
                f" {MIN_POSITIVE}/{N_FORWARD} individually positive. No retry with a"
                f" mixing weight w and no retry with a different metric — the registration"
                f" names both as forbidden, which is why K is {K_PR10} and not unbounded."
                f" KXCPICORE is separately and permanently rejected (+0.0298 nats,"
                f" DM p=0.325, one event carrying 101.8% of the gain)."),
            "pre_declared_risk": (
                "Registered BEFORE the forward window and not available as an excuse"
                " afterwards: the 2021 (−0.25 nats) and 2022 (−0.13 nats) year slices are"
                " NEGATIVE. Those are the acceleration years, when the Cleveland nowcast"
                " systematically underestimated. If the forward window lands in a"
                " re-accelerating inflation regime the anchor is EXPECTED to lag."),
            "verdict": _verdict(
                pr10, PR10_SERIES,
                "The registration falsifies the MoM anchor. Reverting means"
                " nowcast_anchor=False for predict_mom(core=False) — not a new w, not a"
                " new metric."),
        },
    }
    return out


def main():
    import argparse
    import sqlite3

    from prediction_market_macro.config.settings import load_settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", action="store_true",
                    help="print the current model/cpi.py fingerprint, for pinning"
                         " REGISTERED_FINGERPRINT at registration time")
    a = ap.parse_args()
    if a.stamp:
        print(code_fingerprint())
        return
    conn = sqlite3.connect(load_settings().db_path)
    conn.row_factory = sqlite3.Row
    print(json.dumps(run(conn), indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
