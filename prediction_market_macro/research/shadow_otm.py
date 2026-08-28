"""research/shadow_otm.py — PR-13 (#186): count the deep-OTM ladder sell forward.

`docs/PREREGISTER.md` registered ONE hypothesis on 2026-08-28 and this module is the only
thing allowed to grade it. Written the same day as the registration, for the reason #195
established: a registered-but-unwritten scorer is a registration that quietly never
matures, and PR-13's window is the whole evidence.

The registration, verbatim
--------------------------
    改动(候选)  在 T−1h、对 mid 落在 [0.02, 0.35) 的 ladder 腿无条件卖 YES(不看模型)
    判据        自登记日起前向 150 条已结算腿,T−1h 成对每腿盈亏,单边 Wilcoxon,
                Bonferroni 除以 K=26 ⇒ α = 0.00192。且合计为正。
                做不到即证伪:不换区间重试、不换度量重试、不放宽腿数
    K           26(发现期),nominal_p = 0.002 ⇒ bonferroni_p = 0.052

Four things about that text worth saying out loud rather than interpreting later
--------------------------------------------------------------------------------
1. **The unit is the LEG, and the count is of legs, not events.** Legs per event range
   from 1 to 41 across the discovery sample, so an event count would let one wide ladder
   decide the window. `n_forward` below is therefore a leg count and the per-event
   structure is kept only for the cluster bootstrap, which is a readout and grades
   nothing.

2. **The discovery period did not pass its own gate** (`bonferroni_p = 0.052`) and the
   band `[0.02, 0.35)` was chosen after looking. That is recorded in PREREGISTER.md and
   it is the reason this module refuses to score anything before `REGISTERED`: the only
   clean sample this hypothesis will ever have is the forward one, and `score()` drops
   every event that closed before the registration instant rather than reporting a
   flattering combined number.

3. **There is no model in this hypothesis, and that changes the leg universe.** Every
   other shadow scorer replays `model/*.predict` and drops an event when
   `pred.data_horizon` reached past the release. PR-13 reads only the book, so it has no
   such leak channel and no reason to inherit that filter — but the DISCOVERY sample was
   collected through a script that computed a model fair per leg and therefore did
   inherit it. Filtering the forward window the same way would be conditioning on an
   artifact; not filtering it makes the two windows differ. Both are wrong in some
   respect, so the primary is the unfiltered one (it matches the rule being tested) and
   `n_model_filtered_out` reports how many legs the discovery filter would have removed.
   The difference is visible rather than absorbed.

4. **"Ladder" here means CUMULATIVE ladder, and the registry does not mean that.** The
   hypothesis came out of `devig_check3.py` table D under "ladder scope", where a ladder
   is a survival curve: every leg is `print > K_i`, the YES legs nest, and their count is
   the rank of the print. `REGISTRY[s].structure == "ladder"` is a coarser label — it also
   covers `KXWTIW`, whose legs are `between`/`less`/`greater` brackets, i.e. a mutually
   exclusive partition in which exactly one leg can settle YES. Those are different bets:
   in a 15-way partition every leg is cheap by construction and the in-band YES rate is
   pinned by the partition rather than by any longshot bias, and empirically KXWTIW's 99
   in-band legs run at −0.018/leg against the cumulative ladders' +0.053. Scoring the
   forward window on `REGISTRY.structure` would therefore test a different hypothesis from
   the one that was registered — it moves the discovery mean from +0.0526 to +0.0253.
   `event_structure` below is the ex-ante rule, and it reproduces the discovery universe
   exactly (64 events / 156 legs, 81/81 events agreeing). See its docstring.

5. **Selection is on the MID, execution is at the BID.** That is what makes the discovery
   number a tradeable one rather than a mid-to-mid accounting identity, and it is the
   single most reversible choice in the whole construction, so it is pinned in a test:
   `pnl = bid − outcome − taker_fee(bid, 1)`, one contract, entry fee only (Kalshi
   charges nothing at settlement, and `taker_fee` is symmetric in p ↔ 1−p so selling YES
   at `b` and buying NO at `1−b` pay the same).

Why this is a replay and not a live logger
------------------------------------------
Same reason as `shadow_width` and `shadow_claims`: `asof` is a deterministic function of
stored timestamps, so nothing about the entry is chosen after the fact. `asof_for` and
`_events` are IMPORTED from `shadow_width` rather than copied — that module already
mirrors `backtest.replay_series`'s two asof rules and `tests/test_shadow_width.py`
already pins the mirror against the original. A third copy would be a third thing to
drift.

What a PASS authorises
----------------------
Nothing at the order router by itself. `strategy/snipe.py` and `strategy/skill.py`
generate no unconditional sell, and the registration is explicit that a per-event
exposure cap has to be judged FORWARD alongside the strategy because the cap changes the
return distribution. A pass here says the effect survived a clean window; the cap is a
separate registration and the wiring is a user decision.
"""
from __future__ import annotations

import hashlib
import pathlib
from datetime import datetime, timezone

import numpy as np

# The registration. None of these may be edited to make a result pass.
REGISTERED = "2026-08-28T00:00:00+00:00"
N_FORWARD = 150                # registered forward SETTLED LEGS, pooled across series
BAND = (0.02, 0.35)            # YES mid, half-open — see PREREGISTER.md for both edges
OFFSET = "-1h"
K_DISCOVERY = 26               # itemised in docs/PREREGISTER.md PR-13
ALPHA = 0.05
ALPHA_BONFERRONI = ALPHA / K_DISCOVERY          # 0.0019230...

# The discovery numbers, so that a drift in this module against the sample it was built
# from is visible in its own output rather than by someone remembering the table.
# `tests/test_shadow_otm.py` re-derives them from the cached discovery dump under BOTH
# universe rules and asserts they are the same 64/156 — that is the check that `score`
# still measures the thing PR-13 was registered on.
DISCOVERY = {"n_events": 64, "n_legs": 156, "mean_pnl_per_leg": 0.0526,
             "total": 8.20, "yes_rate": 0.0385, "p_pos": 0.997,
             "ci95": [0.0176, 0.0819], "bonferroni_p": 0.052,
             "universe": "cumulative_ladder"}

# Stamped once, at the commit that lands PR-13's scorer. Unlike PR-8/PR-12 this does NOT
# fingerprint a model file: PR-13 reads no model. What it fingerprints is the thing that
# CAN silently change the trade — the fee schedule. A fee change makes every forward leg
# a different bet from every discovery leg, and the paired comparison does not survive it.
REGISTERED_FINGERPRINT = "2d97ed8c5773"
KNOWN_FINGERPRINTS: dict[str, str] = {
    "2d97ed8c5773": "strategy/edge.py as of the commit that landed this scorer "
                    "(2026-08-28). taker_fee = ceil_cents(0.07 * C * p * (1-p)), charged "
                    "on entry only. This is the schedule every DISCOVERY leg was priced "
                    "under and every forward leg must be priced under.",
}


def code_fingerprint() -> str:
    """sha1 prefix of `strategy/edge.py`, which owns `taker_fee`."""
    p = pathlib.Path(__file__).resolve().parent.parent / "strategy" / "edge.py"
    return hashlib.sha1(p.read_bytes()).hexdigest()[:12]


def code_change_note(fp: str | None = None) -> dict:
    fp = fp or code_fingerprint()
    if REGISTERED_FINGERPRINT == "PENDING":
        return {"fingerprint": fp, "registered_fingerprint": None,
                "code_changed_since_registration": None, "change_is_documented": False,
                "note": "registration fingerprint not stamped yet — run --stamp once, at "
                        "the commit that lands PR-13's scorer, and never again."}
    return {"fingerprint": fp, "registered_fingerprint": REGISTERED_FINGERPRINT,
            "code_changed_since_registration": fp != REGISTERED_FINGERPRINT,
            "change_is_documented": fp in KNOWN_FINGERPRINTS,
            "note": KNOWN_FINGERPRINTS.get(fp, (
                "UNDOCUMENTED CHANGE — strategy/edge.py no longer matches any recorded "
                "version. PR-13's PnL is net of `taker_fee`, so establish whether the fee "
                "schedule moved and record the answer in KNOWN_FINGERPRINTS before "
                "reading the numbers below as PR-13's."))}


ONE_SIDED = frozenset({"greater", "greater_or_equal"})


def ladder_series() -> tuple[str, ...]:
    """The registry PREFILTER — every series the registry calls a ladder.

    Not the universe. `REGISTRY.structure` is a series-level label that groups true
    survival ladders with `KXWTIW`'s bracket partition (point 4 of the module docstring),
    so it is used only to skip series that cannot possibly qualify — `categorical`
    (KXFEDDECISION) and anything else — cheaply and without a per-event query. The
    decision is `event_structure`, which is taken per event on the legs themselves.
    """
    from prediction_market_macro.config.registry import REGISTRY
    return tuple(s for s, spec in REGISTRY.items() if spec.structure == "ladder")


def event_structure(legs) -> str:
    """`cumulative_ladder` | `partition` | `unknown` for one event, from the CONTRACT
    DEFINITIONS.

    A cumulative ladder is one-sided: every leg is `print > K_i` (`greater`) or
    `print >= K_i` (`greater_or_equal`), so the YES legs nest and their count is the rank
    of the print. Any `between`, `less` or `custom` leg means the strikes carve the line
    into mutually exclusive brackets and at most one leg can settle YES. That is the
    distinction PR-13's discovery table was computed under.

    Ex ante on purpose. The discovery script's `classify()` decided the same question from
    the settlement — `exactly one YES and mids summing below 1.5` — which cannot be
    evaluated at T−1h and so cannot define a forward universe at all. This reads
    `contracts.strike_type`, which is fixed when the strikes are listed. On the 81
    discovery events the two rules agree 81/81 and select the identical 64 events / 156
    legs / +0.0526 mean / 8.20 total / 0.0385 YES rate, so the swap changes the universe
    definition from unexecutable to executable and changes no number; that identity is
    pinned in `tests/test_shadow_otm.py`.

    A missing `strike_type` is `unknown`, not `partition`. 604 contracts in the db carry a
    NULL — an artifact of the backfill era, all of them closing on or before 2025-02-10
    and none after 2026-01-01, so the forward window sees none. Collapsing them into
    `partition` would still exclude them correctly TODAY while making a future ingest
    regression look like an ordinary bracket market, which is precisely the failure this
    hypothesis cannot afford: it would prune the forward sample silently.
    """
    fams = {(l["strike_type"] or "").strip().lower() for l in legs}
    if not fams or "" in fams:
        return "unknown"
    return "cumulative_ladder" if fams <= ONE_SIDED else "partition"


def leg_pnl(bid: float, outcome: float) -> float:
    """Per-contract PnL of selling one YES at the bid and holding to settlement.

    Selling YES at `b` is buying NO at `1 − b`: capital at risk is `1 − b`, the payoff is
    1 when the leg settles NO, and Kalshi charges the taker fee on ENTRY only. Written as
    `bid − outcome − fee` rather than in NO-space because that is the form the discovery
    script used and the two must not merely agree numerically today.
    """
    from prediction_market_macro.strategy.edge import taker_fee
    return float(bid) - float(outcome) - taker_fee(float(bid), 1)


def score(conn, asof: datetime | None = None, since: str | None = None) -> dict:
    """Every in-band ladder leg of every settled event in the window, with its PnL.

    `since` defaults to `REGISTERED`, which is what makes the forward window forward.
    It is a parameter only so the tests can re-derive the discovery table over the
    discovery window; nothing in `run` may pass it.
    """
    from prediction_market_macro.research.backtest import _market_leg_bar
    from prediction_market_macro.research.shadow_width import _events, asof_for
    now = asof or datetime.now(timezone.utc)
    lo_ts = datetime.fromisoformat(since or REGISTERED)
    lo, hi = BAND
    legs: list[dict] = []
    drops = {"no_book": 0, "no_bid": 0, "out_of_band": 0, "not_cumulative": 0,
             "unknown_structure": 0}
    # Events the registry calls a ladder but whose legs are brackets. Reported rather than
    # silently skipped: this is the whole of the discovery-vs-registry universe gap.
    disagreement: list[dict] = []
    n_events = 0
    for series in ladder_series():
        for ev in _events(conn, series):
            if not (lo_ts <= ev["close_ts"] <= now):
                continue
            kind = event_structure(ev["legs"])
            if kind != "cumulative_ladder":
                drops["not_cumulative"] += len(ev["legs"])
                drops["unknown_structure"] += kind == "unknown"
                disagreement.append({
                    "series": series, "period": ev["key"], "n_legs": len(ev["legs"]),
                    "close": ev["close_ts"].isoformat(),
                    "registry_structure": "ladder", "event_structure": kind,
                    "families": sorted({(l["strike_type"] or "<null>")
                                        for l in ev["legs"]})})
                continue
            a = asof_for(ev)
            picked = []
            for l in ev["legs"]:
                bk = _market_leg_bar(conn, l["ticker"], a)
                if bk is None:
                    drops["no_book"] += 1
                    continue
                if not (lo <= bk["mid"] < hi):
                    drops["out_of_band"] += 1
                    continue
                if bk["bid"] <= 0.0:
                    # Nobody bids: the sale is not executable at any size, and scoring it
                    # at the mid would book the entire spread as profit.
                    drops["no_bid"] += 1
                    continue
                out = 1.0 if l["result"] == "yes" else 0.0
                picked.append({
                    "series": series, "period": ev["key"], "ticker": l["ticker"],
                    "close": ev["close_ts"].isoformat(), "asof": a.isoformat(),
                    "mid": bk["mid"], "bid": bk["bid"], "ask": bk["ask"],
                    "volume": bk["volume"], "staleness_s": bk["staleness_s"],
                    "outcome": out, "pnl": round(leg_pnl(bk["bid"], out), 6)})
            if picked:
                n_events += 1
                legs.extend(picked)
    return {"since": lo_ts.isoformat(), "asof": now.isoformat(), "band": list(BAND),
            "offset": OFFSET, "n_events": n_events, "n_legs": len(legs),
            "drops": drops, "structure_disagreement": disagreement, "legs": legs}


def _cluster_ci(legs: list[dict], reps: int = 5000, seed: int = 23) -> dict | None:
    """Bootstrap the mean PnL/leg by resampling EVENTS, not legs.

    Legs of one event share a print, so they are not independent draws — an unclustered
    interval on 156 legs from 64 events would be roughly sqrt(156/64) too narrow. A
    readout, not the criterion: the registration's test is the Wilcoxon below.
    """
    by_ev: dict[tuple, list[float]] = {}
    for l in legs:
        by_ev.setdefault((l["series"], l["period"]), []).append(l["pnl"])
    ev = [np.asarray(v, dtype=float) for v in by_ev.values()]
    if len(ev) < 4:
        return None
    rng = np.random.default_rng(seed)
    bs = np.empty(reps)
    for i in range(reps):
        pick = rng.integers(0, len(ev), len(ev))
        bs[i] = np.concatenate([ev[j] for j in pick]).mean()
    return {"n_events": len(ev), "reps": reps,
            "ci95": [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))],
            "p_pos": float((bs > 0).mean())}


def _wilcoxon(pnl: list[float]) -> tuple[float | None, float | None]:
    """(statistic, one-sided p) for H1: per-leg PnL > 0.

    One-sample against zero, because the null of "unconditionally sell" is "do not
    trade", whose PnL is exactly zero — there is no second arm to pair against. Zeros are
    dropped, which is the signed-rank test's own convention and matters more here than
    usual: the `[0.00, 0.02)` bin's PnL is identically zero (the spread eats it all) and
    if the band edge were ever widened those legs would enter as free significance.
    """
    nz = [p for p in pnl if p != 0.0]
    if len(nz) < 5:
        return None, None
    from scipy.stats import wilcoxon
    r = wilcoxon(nz, alternative="greater")
    return float(r.statistic), float(r.pvalue)


def _model_filter_count(conn, legs: list[dict]) -> int | None:
    """How many scored legs the DISCOVERY script's model filter would have removed.

    Point 3 of the module docstring, as a number. The discovery collector computed a
    model fair per leg and dropped the whole event when `predict` raised or when
    `pred.data_horizon` reached past the release. PR-13's rule needs no model, so the
    forward window does not inherit that filter — but the size of the difference has to
    be visible or the two windows are being compared while one of them is quietly a
    subset. Returns None if the dispatch cannot be resolved at all.
    """
    import importlib
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
    from prediction_market_macro.research.backtest import _settle_release_ts
    seen: dict[tuple, bool] = {}
    n = 0
    for l in legs:
        key = (l["series"], l["period"])
        if key not in seen:
            spec = REGISTRY[l["series"]]
            disp = SERIES_DISPATCH.get(l["series"])
            ok = False
            if disp is not None:
                try:
                    fn = getattr(importlib.import_module(disp[0]), disp[1])
                    pred = fn(conn, datetime.fromisoformat(l["asof"]), l["period"],
                              series=l["series"])
                    rel = _settle_release_ts(conn, spec, l["period"])
                    ok = not (rel is not None and pred.data_horizon is not None
                              and pred.data_horizon >= rel)
                except Exception:                                # noqa: BLE001
                    ok = False
            seen[key] = ok
        n += not seen[key]
    return n


def run(conn, asof: datetime | None = None, model_filter_readout: bool = True) -> dict:
    """The PR-13 readout. PENDING until `N_FORWARD` forward settled legs exist."""
    now = asof or datetime.now(timezone.utc)
    sc = score(conn, now)
    pnl = [l["pnl"] for l in sc["legs"]]
    total = float(sum(pnl))
    stat, p = _wilcoxon(pnl)
    n_yes = sum(1 for l in sc["legs"] if l["outcome"] == 1.0)
    out = {
        "registered": REGISTERED, "asof": now.isoformat(), "band": list(BAND),
        "offset": OFFSET, "unit": "settled leg",
        "k_discovery": K_DISCOVERY, "alpha": ALPHA,
        "alpha_bonferroni": round(ALPHA_BONFERRONI, 5),
        "discovery": DISCOVERY,
        "code_fingerprint": code_fingerprint(), "code_change": code_change_note(),
        "n_forward": sc["n_legs"], "n_required": N_FORWARD, "n_events": sc["n_events"],
        "drops": sc["drops"],
        "structure_disagreement": sc["structure_disagreement"],
        "n_structure_disagreement": len(sc["structure_disagreement"]),
        # Read by `research/prereg.py` on its own alert channel. Named generically because
        # the channel is generic: "the sample this grader is accumulating is being pruned
        # by something that is not the hypothesis". It fires while the verdict is still
        # PENDING, which is exactly when nothing else would say anything.
        "data_warning": (
            None if not sc["drops"]["unknown_structure"] else
            f"{sc['drops']['unknown_structure']} event(s) in the FORWARD window have a "
            "leg with no strike_type, so `event_structure` cannot tell a cumulative "
            "ladder from a bracket partition and they were excluded. Every known NULL "
            "closes on or before 2025-02-10; a forward one is an ingest regression, and "
            "it prunes PR-13's sample. Fix the ingest and re-run — do NOT read the "
            "numbers below as the registered window until this is zero."),
        "total_pnl": round(total, 6),
        "mean_pnl_per_leg": round(total / len(pnl), 6) if pnl else None,
        "n_yes": n_yes,
        "yes_rate": round(n_yes / len(pnl), 6) if pnl else None,
        "worst_leg": round(min(pnl), 6) if pnl else None,
        "wilcoxon_stat": stat, "wilcoxon_p_one_sided": p,
        "cluster_bootstrap": _cluster_ci(sc["legs"]),
        "by_series": _by_series(sc["legs"]),
        # A replay (one predict per event), and it is read only when the verdict is read.
        # Computing it weekly for the whole PENDING window would make the prereg caller
        # pay a full backtest to print a number nobody looks at yet.
        "n_model_filtered_out": (_model_filter_count(conn, sc["legs"])
                                 if model_filter_readout
                                 and sc["n_legs"] >= N_FORWARD else None),
    }
    out["verdict"] = _verdict(out)
    out["criterion"] = (
        f"PR-13, K={K_DISCOVERY}: {N_FORWARD} forward settled CUMULATIVE-ladder legs from "
        f"{REGISTERED[:10]} whose YES mid at {OFFSET} sits in [{BAND[0]}, {BAND[1]}), "
        f"sold at the BID net of taker fee; one-sided Wilcoxon on per-leg PnL against "
        f"zero at Bonferroni alpha={ALPHA_BONFERRONI:.5f}, AND a positive total. "
        "Failing it falsifies the hypothesis: per the registration there is no retry at "
        "another band, another metric or a smaller leg count. A pass authorises nothing "
        "at the order router on its own — the per-event exposure cap is a separate "
        "registration, because a cap changes the return distribution it would be judged "
        "on.")
    return out


def _by_series(legs: list[dict]) -> dict:
    """Per-series totals. A readout with a specific job: the discovery period's entire
    margin hung on KXAAAGASW (41/156 legs, 2.89 of 8.20 total; leaving it out put the CI
    lower bound at −0.0004). If the forward window repeats that shape it is one series,
    not a market-wide longshot bias, whatever the pooled p-value says."""
    out: dict[str, dict] = {}
    for l in legs:
        r = out.setdefault(l["series"], {"n_legs": 0, "total": 0.0, "n_yes": 0})
        r["n_legs"] += 1
        r["total"] += l["pnl"]
        r["n_yes"] += int(l["outcome"] == 1.0)
    for r in out.values():
        r["total"] = round(r["total"], 6)
        r["mean"] = round(r["total"] / r["n_legs"], 6)
    return out


def _verdict(o: dict) -> str:
    if o["n_forward"] < o["n_required"]:
        return (f"PENDING — {o['n_forward']}/{o['n_required']} forward settled legs. No "
                "verdict, and the totals above are a progress readout, not a result.")
    p, tot = o["wilcoxon_p_one_sided"], o["total_pnl"]
    if p is None:
        return ("INCONCLUSIVE — fewer than 5 non-zero legs, so the signed-rank test has "
                "no power. Not a pass and not a falsification; the sample is degenerate.")
    if tot > 0 and p < ALPHA_BONFERRONI:
        return (f"PASSED — {o['n_forward']} forward legs, total {tot:+.2f}, "
                f"{o['mean_pnl_per_leg']:+.4f}/leg, one-sided p={p:.5f} < "
                f"{ALPHA_BONFERRONI:.5f}. Authorises nothing at the router by itself: the "
                "per-event exposure cap is a separate forward registration.")
    why = "the total is not positive" if tot <= 0 else f"p={p:.5f} >= {ALPHA_BONFERRONI:.5f}"
    return (f"FALSIFIED — {o['n_forward']} forward legs, total {tot:+.2f}, {why}. Per the "
            "registration there is no re-search at another band, metric or leg count.")


def main():
    import argparse
    import json

    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--since", default=None,
                    help="override the window start — for reproducing the DISCOVERY "
                         "table only; run() never passes it")
    ap.add_argument("--legs", action="store_true", help="include the per-leg rows")
    ap.add_argument("--stamp", action="store_true",
                    help="print the fingerprint to paste into REGISTERED_FINGERPRINT")
    a = ap.parse_args()
    if a.stamp:
        print(code_fingerprint())
        return
    conn = init_db(a.db or load_settings().db_path)
    if a.since:
        sc = score(conn, since=a.since)
        pnl = [l["pnl"] for l in sc["legs"]]
        sc["mean_pnl_per_leg"] = sum(pnl) / len(pnl) if pnl else None
        sc["total_pnl"] = sum(pnl)
        sc["cluster_bootstrap"] = _cluster_ci(sc["legs"])
        sc["by_series"] = _by_series(sc["legs"])
        if not a.legs:
            sc.pop("legs")
        print(json.dumps(sc, ensure_ascii=False, indent=1, default=str))
        return
    out = run(conn)
    if not a.legs:
        out.pop("legs", None)
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
