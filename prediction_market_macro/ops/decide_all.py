"""ops/decide_all.py — §8.0 step 3: daily scan/decide (paper) on every fresh pred.

Pipeline per (series, period): latest pred → contracts+latest quotes → market-implied
ladder devig (violations → alerts: free-money monotonicity arbs) → enumerate structures
→ decide() gates → ledger. Freeze windows honored via the releases table.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.jobs.scheduler import set_coverage
from prediction_market_macro.ops import ledger
from prediction_market_macro.strategy import devig
from prediction_market_macro.strategy.decision import decide
from prediction_market_macro.strategy.edge import enumerate_structs
from prediction_market_macro.util.periods import kalshi_period_to_key

# §8.2-5 staleness hard gate. Named rather than inlined because `jobs/tick.py` has to
# top the quotes up to exactly this bar before it calls `run()` — a second copy of the
# number there would drift silently and re-open the hole it exists to close.
PRED_STALE_H = 26.0
QUOTE_STALE_H = 6.0


def _structs_categorical(legs: list[dict], probs: dict[str, float]):
    """Single-leg YES/NO structures for mutually-exclusive category markets
    (leg category = ticker suffix after the last '-', e.g. ...-H0 → 'H0')."""
    from prediction_market_macro.strategy.edge import Leg, Struct
    out = []
    for l in legs:
        cat = l["ticker"].rsplit("-", 1)[-1]
        if cat not in probs:
            continue
        fair = float(probs[cat])
        if l.get("yes_ask") is not None and 0 < l["yes_ask"] < 1:
            out.append(Struct("single", (Leg(l["ticker"], "yes", l["yes_ask"],
                                             l["ask_depth"]),),
                              fair, l["yes_ask"], l["yes_ask"],
                              f"YES {l['ticker']} @{l['yes_ask']:.2f}"))
        if l.get("yes_bid") is not None and 0 < l["yes_bid"] < 1:
            np_ = round(1 - l["yes_bid"], 4)
            out.append(Struct("single", (Leg(l["ticker"], "no", np_, l["bid_depth"]),),
                              1 - fair, np_, np_, f"NO {l['ticker']} @{np_:.2f}"))
    return out


def _legs_meta(conn, series: str, kalshi_tok: str) -> list[dict]:
    rows = conn.execute(
        "SELECT c.ticker, c.floor_strike strike, c.cap_strike, c.strike_type, c.close_time,"
        " q.yes_bid, q.yes_ask, q.bid_depth, q.ask_depth "
        "FROM contracts c LEFT JOIN quotes q ON q.ticker=c.ticker AND q.ts="
        " (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker) "
        "WHERE c.series=? AND c.period=? AND c.status='active'",
        (series, kalshi_tok)).fetchall()
    return [dict(r) for r in rows]


def _has_any_open(conn, series: str, period: str) -> bool:
    # #149. This one was already BALANCED (all open kinds vs all close kinds) and so
    # agreed with the truth on all 66 live periods — it is the reference the other three
    # were measured against. Routed through `open_decisions` anyway: leaving the one
    # correct implementation as the only private copy is how the set drifts apart again.
    from prediction_market_macro.ops.ledger import open_decisions
    return bool(open_decisions(conn, series, period))


def argmax_candidate(structs):
    """The structure the argmax leg would buy, BEFORE the defer-to-market filter.

    Split out for PR-2 (#126): the filter's two arms have to be looking at the same
    candidate, and the only way to guarantee that is for both to call this. Note the
    ordering it encodes — select THEN filter. Relaxing the filter therefore never changes
    WHICH structure is bought, only whether it is bought at all, which is what makes the
    two arms a clean paired comparison.
    """
    cands = [st for st in structs if 0.10 <= st.cost <= 0.90 and st.fair > 0.5]
    return max(cands, key=lambda x: x.fair) if cands else None


def defers_to_market(st) -> bool:
    """PR-2's rule under test: skip the favourite when our fair exceeds the ask.

    The original justification was "dual-window validated 27W-2L" — but that was measured
    BEFORE #109 rebuilt the PIT gates and before the bucket devig fix (#127), so it is not
    evidence about the strategy that runs today. #126 re-validates it forward; until then
    the rule stays ON and unchanged.
    """
    return st.fair > st.cost


def argmax_sizing(st) -> tuple[int, float]:
    """(count, size_usd) for the flat-$1 favourite leg.

    Sizes against the FILL, not the quote (`strategy/edge.py::fill_price`) — the flat +1c
    this path used to apply after sizing broke the $1 cap on every cheap leg.
    """
    count = max(1, int(1.0 / st.cost))
    count = max(1, int(1.0 / max(st.fill_cost(count), 0.01)))
    return count, round(st.fill_cost(count) * count, 2)


def _shadow_argmax(conn, spec, key: str, st, count: int, size: float,
                   deferred: bool, now) -> None:
    """PR-2 (#126): record the argmax leg the filter saw. **Never writes `decisions`.**

    One row per (series, period) — the argmax stream places at most one leg per event, so
    the primary key is the natural one and a re-run inside the same cycle is idempotent
    rather than double-counted.

    BOTH arms are written, not just the deferred one. The comparison is filter-on vs
    filter-off, and filter-on's trades are the `arm='placed'` rows; recording only the
    skipped trades would leave the scorer to reconstruct the other arm from `decisions`
    later, and reconstruction after settlement is the researcher degree of freedom this
    whole shadow apparatus exists to remove.

    `legs_json` stores FILL prices (`fill_prices(count)`), which is what the trade would
    actually have paid, so `research/shadow_pr2` can settle the row without re-deriving a
    price from a book that no longer exists.
    """
    conn.execute(
        "INSERT OR REPLACE INTO shadow_argmax(ts_utc, series, period, arm, fair, cost,"
        " count, size_usd, desc, legs_json, note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), spec.ticker, key, "deferred" if deferred else "placed",
         float(st.fair), float(st.cost), int(count), float(size), st.desc,
         json.dumps([{"ticker": l.ticker, "side": l.side, "price": px}
                     for l, px in zip(st.legs, st.fill_prices(count))]),
         f"fair={st.fair:.4f} cost={st.cost:.4f} "
         f"{'deferred (fair>cost)' if deferred else 'placed (fair<=cost)'}"))


def _place_argmax(conn, spec, key: str, structs, now, note_extra: str = "") -> bool:
    """WC-hybrid favourite leg: no edge cleared ⇒ buy the MODEL's most likely
    structure flat $1 (kind='argmax'). Price window [0.10, 0.90] keeps payoff
    room net of fees; one per (series, period); risk limits still apply.

    Also writes PR-2's shadow row (#126) for BOTH arms. `risk.check` is applied before the
    defer-to-market filter is consulted so that the deferred arm is held to the same
    admission standard as the placed one — it is a pure read (no writes, no commit), so
    consulting it earlier changes nothing about what this function does. Without that, a
    deferred trade that risk would have rejected anyway would still be credited to the
    no-filter arm, which is the asymmetric-sample failure `shadow_claims` guards against
    on the other registration.
    """
    from prediction_market_macro.ops import risk
    st = argmax_candidate(structs)
    if st is None or _has_any_open(conn, spec.ticker, key):
        return False
    count, size = argmax_sizing(st)
    if risk.check(conn, spec.ticker, key, size) is not None:
        return False
    # #148 — do not open what `ops/exits` rule 1 closes on the same cycle. Sits here, with
    # `risk.check` and ahead of the shadow write, for the reason the docstring above gives
    # about `risk.check`: it is an ADMISSION standard, so holding only the placed arm to it
    # would leave PR-2 crediting the deferred arm with trades that could never have been
    # held. It is a pure read, like `risk.check`. See `exits.opens_into_exit` for the
    # measurement (3 of the 4 live argmax legs round-tripped in 219ms) and for why the
    # repair is on this side rather than on the exit.
    from prediction_market_macro.ops import exits as _exits
    if _exits.opens_into_exit(st, _exits.struct_mid_cost(conn, st)):
        return False
    deferred = defers_to_market(st)
    _shadow_argmax(conn, spec, key, st, count, size, deferred, now)
    if deferred:
        return False
    now_iso = now.isoformat()
    from prediction_market_macro.strategy.edge import taker_fee
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, fair,"
        " ask, net_edge, size_usd, inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now_iso, spec.ticker, key,
         json.dumps({"kind": "argmax", "desc": st.desc,
                     "legs": [{"ticker": l.ticker, "side": l.side,
                               "price": l.price} for l in st.legs]}),
         "argmax", st.fair, st.cost, st.net_edge(), size,
         json.dumps({"stream": "argmax"}), "argmax/1.0", "{}",
         f"ARGMAX fav fair={st.fair:.3f} {note_extra}".strip()))
    from prediction_market_macro.ops import trading_kalshi
    did_argmax = cur.lastrowid
    for leg, px in zip(st.legs, st.fill_prices(count)):
        curf = conn.execute(
            "INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count,"
            " fee_usd, mode) VALUES(?,?,?,?,?,?,?, 'paper')",
            (did_argmax, now_iso, leg.ticker, leg.side, px, count,
             taker_fee(px, count)))
        trading_kalshi.on_fill(conn, curf.lastrowid)   # §30.3 inline mirror
    conn.commit()
    return True


_AAA_ANCHOR_MAX_AGE_D = 2


def _aaa_information_gate(conn, pr, now: datetime) -> str | None:
    """None if this pred may trade; otherwise the reason it may not (§27.4, §5-bis 口5).

    KXAAAGASW settles on AAA's daily national average, which the other side of the market
    can read the same day. Our own AAA_DAILY feed only begins 2026-07-31 and is a
    page scrape that can never be backfilled, so whenever `model/energy.py` falls back to
    the WEEKLY GASREGW proxy we are knowingly quoting a number our counterparty already
    knows. That is how #743 lost: market 1%, us 26%, settled NO — and the post-mortem
    found the model honestly calibrated the whole time (LOO 0.0862 IS the real error of a
    weekly proxy). No distribution fix reaches an information gap; the only correct
    response is to not trade the series in that state.

    Two conditions, both required: the pred must have come from the daily-anchor branch,
    and our feed must actually be alive. AAA publishes 7 days a week, so the age check
    fires on OUR collector being down, not on a weekend.

    This was documented as "deliberately live-lane only — the backtest needs no equivalent,
    since every AAA_DAILY row carries knowledge_time >= 2026-07-31T19:46Z, so any replay
    before then sees an empty series and takes the proxy branch by construction."

    That premise is true and the conclusion drawn from it was backwards. **The proxy branch
    is the blocked branch**, so a replay before 2026-07-31 takes the exact state this gate
    exists to refuse — and the backtest, having no equivalent, booked it. All 6 KXAAAGASW
    trades in the displayed d75 window are dated 05-24..06-23, i.e. every one of them.
    `research/walkforward.py` now applies the same test right after the pred is built
    (it needs `mode`, which is why it cannot sit as early as this one does).
    """
    if pr["series"] != "KXAAAGASW":
        return None
    try:
        mode = (json.loads(pr["inputs_json"] or "{}") or {}).get("mode")
    except ValueError:
        return "aaa_inputs_unreadable"
    if mode != "aaa_daily_anchor":
        return f"aaa_proxy_only mode={mode} — settle source not visible to us (§27.4)"
    r = conn.execute("SELECT MAX(event_time) m FROM fred_obs WHERE sid='AAA_DAILY'"
                     " AND knowledge_time<=?", (now.isoformat(),)).fetchone()
    if not r or not r["m"]:
        return "aaa_daily_missing"
    age = (now.date() - datetime.fromisoformat(r["m"]).date()).days
    if age > _AAA_ANCHOR_MAX_AGE_D:
        return f"aaa_daily_stale {age}d > {_AAA_ANCHOR_MAX_AGE_D}d"
    return None


def _bankroll(conn, settings) -> float:
    """Live demo-account balance (cached in db by the refresh bankroll step);
    static seed only if never fetched."""
    from prediction_market_macro.venues.kalshi.account import current_bankroll
    return current_bankroll(conn)


def _warn_unevaluated_series_gate(conn) -> None:
    """Say out loud which series have no stored §25.4 verdict, once per day.

    `series_enable.blocked` fails open on a missing artefact, which is the right default
    — but on 2026-08-06 EVERY series was missing one (the module shipped 2026-08-05, the
    day after the last `eval.run_all` pass wrote the artefacts), so the gate had never
    once fired live while the published d75 backtest, which recomputes the same fold
    PIT-wise, recorded `series_disabled: 13`. Nothing anywhere reported the difference.

    Same shape and same reasoning as `risk._breaker_blind`: warn, do not trip. This can
    only mean "the weekly eval has not run yet", never "stop trading".
    """
    from prediction_market_macro.strategy import series_enable
    missing = series_enable.unevaluated(conn, [s.ticker for s in REGISTRY.values()])
    if not missing:
        return
    # #155: while SHADOW is on the gate cannot fire for a SECOND reason, and saying only
    # the first would misreport a deliberate choice as a missing job.
    why = ("§25.4 is in SHADOW mode and would not fire in any case"
           if series_enable.SHADOW else "§25.4 cannot fire until weekly_eval_gates runs")
    msg = (f"series_enable gate unevaluated for {len(missing)}/{len(REGISTRY)} series"
           f" ({','.join(missing)}) — {why}")
    today = datetime.now(timezone.utc).date().isoformat()
    if conn.execute("SELECT 1 FROM alerts WHERE source='series_enable' AND message=?"
                    " AND ts>=? LIMIT 1", (msg, today)).fetchone():
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(), "warn", "series_enable", msg))
    conn.commit()


def run(conn, settings) -> int:
    now = datetime.now(timezone.utc)
    n = 0
    _warn_unevaluated_series_gate(conn)
    for spec in REGISTRY.values():
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)).fetchall():
            tok = r["period"]
            key = kalshi_period_to_key(tok)
            if not key:
                continue
            # ── model-free legs first (2026-08-20 gate-ordering fix) ─────────────
            # The devig consistency scan and §24-A's arb execution need NO prediction,
            # so they are hoisted above the three MODEL gates. The old order put arb
            # behind `pr is None`, the §8.2-5 staleness gate and §27.4's information
            # gate. None of those is a statement about the book: an arb says two of
            # these QUOTES contradict each other, and a missing prediction, a stale
            # one, or a model that is late to a gasoline print can neither create that
            # contradiction nor resolve it. Measured over the ledger, 29% of detected
            # violations died on a gate with nothing to say about them.
            #
            # What deliberately stays ABOVE arb:
            #   * the circuit breaker (铁律 10). "Tripped series open NOTHING new" is
            #     about capital and about our own confidence in the whole series, not
            #     about models — and an arb is still a new position, still capital, and
            #     still riskless only if the settlement source is behaving. Hoisting
            #     arb above THIS would trade while halted, which is the one ordering
            #     the rule exists to forbid.
            #   * quote staleness. This one IS about the book, so arb answers to it
            #     below — just to its own half of the gate, not to the pred half.
            legs = _legs_meta(conn, spec.ticker, tok)
            if not legs:
                continue
            # production-model guard: only the registry-bound model's preds may drive
            # decisions — shadow members (chronos2/*, dfm/*) are analysis-only (§7-bis).
            # Read here, but NOT yet a `continue`: the arb leg below runs without it.
            pr = conn.execute(
                "SELECT * FROM preds WHERE series=? AND period=? AND model_version LIKE ?"
                " ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            # circuit breaker (铁律 10): tripped series open NOTHING new
            from prediction_market_macro.ops import risk
            trip = risk.breaker_tripped(conn, spec.ticker)
            if trip:
                # the ledger row is a MODEL decision ("we would have looked and we
                # passed"), so it is still conditional on there being a model to speak
                # for. With no pred this used to `continue` one line earlier and write
                # nothing; it still writes nothing.
                if pr is not None:
                    from prediction_market_macro.strategy.decision import Decision, GATES
                    ledger.record(conn, series=spec.ticker, period=key,
                                  decision=Decision("pass", None, 0.0, 0,
                                                    (f"circuit_breaker [{trip[:100]}]",),
                                                    dict(GATES)),
                                  pred_inputs={}, model_version=pr["model_version"])
                    set_coverage(conn, spec.ticker, key, "passed")
                    n += 1
                continue
            # freshest quote on this book — read once and used by BOTH the arb leg
            # (which is only about the book) and the §8.2-5 gate below (which is about
            # the book AND the prediction).
            qt = conn.execute(
                "SELECT MAX(q.ts) m FROM quotes q JOIN contracts c ON c.ticker=q.ticker"
                " WHERE c.series=? AND c.period=?", (spec.ticker, tok)).fetchone()
            quote_age_h = ((now - datetime.fromisoformat(qt["m"])).total_seconds()
                           / 3600.0) if qt and qt["m"] else None
            impl = None
            if spec.structure != "categorical":
                is_bucket = any(l.get("strike_type") == "between" for l in legs)
                impl = (devig.bucket_implied(legs) if is_bucket
                        else devig.ladder_implied(legs))
                for v in impl["violations"]:
                    msg = (f"PARTITION-ARB {spec.ticker}/{tok}: {v['kind']}"
                           f" gross {v['gross']}" if "kind" in v else
                           f"MONOTONE-ARB {spec.ticker}/{tok}: buy {v['buy']['ticker']}"
                           f" sell {v['sell']['ticker']} gross {v['gross']}")
                    dup = conn.execute(
                        "SELECT 1 FROM alerts WHERE source='consistency' AND"
                        " message=? AND ts>=?",
                        (msg, now.date().isoformat())).fetchone()
                    if not dup:               # standing arbs re-detected per run
                        conn.execute(
                            "INSERT INTO alerts(ts, level, source, message)"
                            " VALUES(?,?,?,?)",
                            (now.isoformat(), "info", "consistency", msg))
                # §24-A: detected free money gets TRADED (paper), not just alerted
                if impl["violations"]:
                    from prediction_market_macro.strategy import arb
                    if quote_age_h is not None and quote_age_h <= QUOTE_STALE_H:
                        n += arb.execute(conn, spec.ticker, key, legs,
                                         impl["violations"])
                    else:
                        # the one gate an arb DOES answer to, and it gets a line rather
                        # than a silent drop: "the book is too old to trust" and "there
                        # was no violation" are different facts, and this module spent
                        # months unable to tell them apart.
                        _qage = ("missing" if quote_age_h is None
                                 else f"{quote_age_h:.1f}h old")
                        arb._alert_once(
                            conn,
                            f"ARB-STALE-BOOK {spec.ticker}/{tok}:"
                            f" {len(impl['violations'])} violation(s) detected but the"
                            f" freshest quote is {_qage} (limit {QUOTE_STALE_H}h) —"
                            f" a 'riskless' trade priced off a book that may have moved"
                            f" is just a bet that it did not", "warn")
            if pr is None:
                continue
            # staleness hard gate (§8.2-5): stale inputs ⇒ forced PASS, never a
            # decision on old data. Pred >26h or freshest quote >6h old ⇒ stale.
            pred_age_h = (now - datetime.fromisoformat(pr["asof"])
                          ).total_seconds() / 3600.0
            if (pred_age_h > PRED_STALE_H or quote_age_h is None
                    or quote_age_h > QUOTE_STALE_H):
                from prediction_market_macro.strategy.decision import Decision, GATES
                reason = (f"stale_inputs pred={pred_age_h:.0f}h"
                          f" quotes={quote_age_h if quote_age_h is None else round(quote_age_h, 1)}h")
                ledger.record(conn, series=spec.ticker, period=key,
                              decision=Decision("pass", None, 0.0, 0, (reason,),
                                                dict(GATES)),
                              pred_inputs={}, model_version=pr["model_version"])
                set_coverage(conn, spec.ticker, key, "passed")
                n += 1
                continue
            # information-disadvantage gate (§27.4) — see _aaa_information_gate
            blind = _aaa_information_gate(conn, pr, now)
            if blind:
                from prediction_market_macro.strategy.decision import Decision, GATES
                ledger.record(conn, series=spec.ticker, period=key,
                              decision=Decision("pass", None, 0.0, 0, (blind,),
                                                dict(GATES)),
                              pred_inputs={}, model_version=pr["model_version"])
                set_coverage(conn, spec.ticker, key, "passed")
                n += 1
                continue
            if spec.structure == "categorical":
                dist = json.loads(pr["dist_json"])
                probs = dist.get("probs") or {}
                structs, impl = _structs_categorical(legs, probs), {"pmf": None,
                                                                    "violations": []}
            else:
                # `impl` was already built above, before the model gates, because the
                # arb leg needs it and does not need them. Reusing it is not just a
                # saved devig call: recomputing here would re-derive the market pmf
                # from a book read at a different instant than the one arb priced
                # against, so the §19-4 market fair and the trade would disagree about
                # what the book said. It is not None on this branch by construction —
                # the same `spec.structure != "categorical"` test built it.
                assert impl is not None
                if not pr["ladder_json"]:
                    continue
                pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
                structs = enumerate_structs(legs, pmf, strict=spec.strict_gt)
            # §19-3: Kelly and every gate consume CALIBRATED probabilities
            from prediction_market_macro.strategy.calibration import calibrate_structs
            structs = calibrate_structs(conn, spec.ticker, structs)
            # §19-4 support signals: normalized entropy + devigged market fair per
            # struct + per-strike capture memory
            import math as _math
            entropy_norm, market_fairs, model_mean = None, None, None
            if spec.structure != "categorical" and pr["ladder_json"]:
                pmf_probs = [v for v in pmf.values() if v > 0]
                if len(pmf_probs) > 1:
                    h = -sum(p * _math.log(p) for p in pmf_probs)
                    entropy_norm = h / _math.log(len(pmf_probs))
                model_mean = sum(k * v for k, v in pmf.items())
                if impl.get("pmf"):
                    mk_pmf = {float(k): v for k, v in impl["pmf"].items()}
                    market_fairs = {ms.desc: ms.fair for ms in
                                    enumerate_structs(legs, mk_pmf,
                                                      strict=spec.strict_gt)}
            elif spec.structure == "categorical":
                pvals = [v for v in probs.values() if v > 0]
                if len(pvals) > 1:
                    h = -sum(p * _math.log(p) for p in pvals)
                    entropy_norm = h / _math.log(len(pvals))
            from prediction_market_macro.strategy import capture as cap_mod
            caps = cap_mod.load_strike_capture(conn, spec.ticker)
            if caps and model_mean is not None:
                strikes = {l["ticker"]: l.get("strike") for l in legs}
                structs, cap_drops = cap_mod.filter_structs(
                    structs, caps, model_mean, spec.round_rule, strikes)
                for cd in cap_drops:
                    conn.execute(
                        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                        (now.isoformat(), "info", "capture_gate",
                         f"{spec.ticker}/{key}: {cd}"))
            rel = conn.execute("SELECT scheduled_ts FROM releases WHERE cal=? AND period=?",
                               (spec.calendar, key)).fetchone()
            release_ts = datetime.fromisoformat(rel["scheduled_ts"]) if rel else None
            closes = [l["close_time"] for l in legs if l.get("close_time")]
            close_ts = min((datetime.fromisoformat(c.replace("Z", "+00:00")) for c in closes),
                           default=None)
            # §19-8: active structural-break flags tighten the gates for this family
            from prediction_market_macro.analysis.llm import active_flags
            from prediction_market_macro.strategy.decision import GATES as _G
            gates_eff = dict(_G)
            flags = active_flags(conn, spec.family)
            if flags:
                sev = max(f["severity"] for f in flags)
                gates_eff["max_size_usd"] = _G["max_size_usd"] * 0.5
                gates_eff["max_entropy_norm"] = _G["max_entropy_norm"] - 0.02 * sev
            # §23.2-4: ACI conformal throttle — model outside its own error
            # envelope ⇒ halve size until it re-enters
            from prediction_market_macro.strategy import conformal
            gates_eff["max_size_usd"] *= conformal.sizing_factor(conn, spec.ticker)
            # skill-aware defense: trailing OOS Brier behind the market ⇒ the
            # computed edge is mostly model error — double the bar, halve the size
            from prediction_market_macro.strategy import skill
            blk = skill.blocked(conn, spec.ticker)
            if blk is not None:
                # WAY behind (ratio>1.5): model-path bets are fee donations. Pass
                # outright; scoring keeps accruing from preds (no bet required),
                # and arb/snipe (structural edges) already executed above.
                from prediction_market_macro.strategy.decision import Decision
                ledger.record(conn, series=spec.ticker, period=key,
                              decision=Decision("pass", None, 0.0, 0,
                                                (f"skill_blocked ratio={blk:.2f}",),
                                                gates_eff),
                              pred_inputs={}, model_version=pr["model_version"])
                set_coverage(conn, spec.ticker, key, "passed")
                n += 1
                continue
            # §25.4 per-series switch. Deliberately AFTER the skill block and shaped the
            # same way (record a pass, `continue`, argmax leg suppressed too): it is one
            # more veto, never a release. Placing it before the skill block would change
            # nothing about which trades happen but would make the recorded reason the
            # weaker of the two on a series both gates reject, and the ledger's reason
            # string is the only record of why a bet did not happen.
            from prediction_market_macro.strategy import series_enable
            # #155: one load feeding both the record and the action, so the two can never
            # describe different rows if the weekly eval lands mid-cycle. `veto` is None
            # while `series_enable.SHADOW` is on — the recorded verdict is then the whole
            # output of this gate, which is the intent.
            se_state = series_enable.state(conn, spec.ticker)
            series_enable.record_shadow(conn, spec.ticker, se_state, now)
            off = series_enable.veto(se_state)
            if off:
                from prediction_market_macro.strategy.decision import Decision
                ledger.record(conn, series=spec.ticker, period=key,
                              decision=Decision("pass", None, 0.0, 0, (off,), gates_eff),
                              pred_inputs={}, model_version=pr["model_version"])
                set_coverage(conn, spec.ticker, key, "passed")
                n += 1
                continue
            sk = skill.defensive(conn, spec.ticker)
            if sk is not None:
                gates_eff["min_net_edge"] *= 2.0
                gates_eff["max_size_usd"] *= 0.5
                gates_eff["fav_min_edge_per_day"] = \
                    gates_eff.get("fav_min_edge_per_day", 0.008) * 2.0
            # §23.2-3a: a wide book makes the devigged market prob noisy. The original
            # response was `market_fairs = None`, which is not neutral — it sends
            # `decide()`'s sanity gate to its `st.cost` fallback, i.e. it compares our
            # fair against the very ask we are about to pay. On a wide book that ask sits
            # far above the devigged prob, so the fallback LOOSENED the one check
            # `decide()` documents as unconditional, exactly where the quote is least
            # trustworthy. #130 measured it on the 75-day run: the branch fired on 10 of
            # 52 trades, and 9 of them showed a devigged gap of 0.27-0.49 against an
            # ask-based gap of 0.06-0.24, all comfortably under the 0.25 ceiling.
            #
            # Keep the gate unconditional without pretending the noisy number is precise:
            # per structure, reference whichever of the two is FURTHER from our fair. A
            # wide quote can then only tighten the gate, never loosen it, it introduces
            # no new constant, and when devig is unavailable altogether the expression
            # collapses to `st.cost` — today's behaviour.
            from prediction_market_macro.model.ensemble import WIDE_SPREAD, median_spread
            sp = median_spread(legs)
            if sp is not None and sp > WIDE_SPREAD:
                _mf = market_fairs or {}
                market_fairs = {st.desc: max(_mf.get(st.desc, st.cost), st.cost,
                                             key=lambda p, f=st.fair: abs(f - p))
                                for st in structs}
            d = decide(structs, now=now, close_time=close_ts, release_ts=release_ts,
                       market_implied=market_fairs,
                       already_open=ledger.has_open(conn, spec.ticker, key),
                       bankroll=_bankroll(conn, settings), entropy_norm=entropy_norm,
                       gates=gates_eff)
            if d.action == "open":
                from prediction_market_macro.ops import risk
                veto = risk.check(conn, spec.ticker, key, d.size_usd)
                if veto is not None:
                    from prediction_market_macro.strategy.decision import Decision
                    d = Decision("pass", None, 0.0, 0, (veto.reason,), d.gate_snapshot)
            ledger.record(conn, series=spec.ticker, period=key, decision=d,
                          pred_inputs=json.loads(pr["dist_json"]),
                          model_version=pr["model_version"])
            # WC-hybrid second leg: edge stream passed ⇒ favourite (argmax) flat
            # bet inside the entry window (freeze/close gates already vetted)
            if d.action == "pass" and close_ts is not None:
                dtc = (close_ts - now).total_seconds() / 86400.0
                if 0.03 <= dtc <= gates_eff.get("max_days_to_close", 7.0):
                    _place_argmax(conn, spec, key, structs, now)
            set_coverage(conn, spec.ticker, key,
                         "decided" if d.action == "open" else "passed")
            n += 1
    conn.commit()
    return n
