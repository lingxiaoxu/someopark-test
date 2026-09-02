"""research/pnl_score.py — score one event under one parameter set by what the STRATEGY
would have made on it, not by how well the model forecast it.

**Why this exists.** `param_wf.brier` averages the squared error over every leg of the
ladder — 10 to 20 of them on a CPI event. The strategy touches exactly one: the structure
`decide()` opens, or the favourite the argmax leg buys. So a parameter set can sharpen the
far tails, improve mean Brier, and not move a single bet. The three-window result in
DECISION_RULE_113.md answers "do these parameters forecast better"; this module exists
because the question asked was "do these parameters make the hybrid strategy more money".

**The hybrid is the live rule, and it is reproduced here exactly.**
`ops/decide_all.py:334-353`: the edge stream runs first, and the favourite (argmax) leg is
placed only when the edge stream PASSED and we are inside the entry window. So per
(series, period) the strategy holds the edge trade if it fired, otherwise the favourite,
otherwise nothing. `walkforward.py:321` reconstructs the same rule from its two separately
accounted streams; this module computes it directly.

**Entry timing is replayed, not assumed.** The 60d walk-forward enters overwhelmingly at
the 7-day gate boundary (majority of trades at 5.5-6.9d lead) and that is where the PnL
lives (+10.99 in the 3-7d bucket against -2.24 in 1-3d). Scoring at a single fixed asof —
close-1h, as the Brier path does — would therefore score a trade the strategy does not
make, at prices it never sees. So each event is replayed forward one simulated day at a
time over its own entry window and the FIRST day the gates clear is the entry, which is
the live no-averaging rule.

**Day-independence, and why it still permits build-once-then-mask.** The replay window of
an event is derived from that event's own close time, so its score does not depend on
which simulated day the selection has reached, exactly as the Brier matrix does not. A
selection at day D remains a mask over events that closed strictly before D. What is
deliberately NOT reproduced is `fair_mode='pooled'`, whose pool weights accumulate across
events and would make an event's score depend on the ones before it; production runs
`model` mode (every stored `daily_walkforward` row is `d{N}:model`) and that is what is
replayed.

The market side is the binding constraint on this objective and is not talked around: only
63 settled events across all 14 series have a stored candle before their own close, all of
them after 2026-05, at most 11 for any one series. `dsr.MIN_OBS` is 12. Per-series
selection on realised PnL therefore cannot clear the floor on this database, and that is a
fact about the data rather than something to be engineered around.
"""
from __future__ import annotations

import importlib
import math
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY, strict_gt_at
from prediction_market_macro.model.common import Categorical, grid_pmf
from prediction_market_macro.ops.decide_all import _structs_categorical
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.strategy.decision import GATES, decide
from prediction_market_macro.strategy.edge import enumerate_structs, taker_fee

OFFSET_HOUR = 16          # 16:00 UTC ~ noon ET, the walkforward simulated-day stamp
BANKROLL = 100.0

# (id(conn), series) -> (conn, GateHistory). See `gate_history` for why the connection is
# kept in the value and not just used as a key. Process-lifetime; every caller of this
# module is a batch job (`param_select.refresh`, `param_wf`, the tests), not a daemon.
_GATE_HIST: dict[tuple[int, str], tuple[object, object]] = {}


def wf_gates(max_lead_days: float | None = None) -> dict:
    """The base gate set `walkforward.run` uses, before the per-day db-state layer.

    Depth is waived because daily candles carry no book depth. The db-state gates
    (skill / calibration / capture / conformal) are NOT dropped here any more — see
    `gate_history` below.
    """
    g = dict(GATES)
    g["min_leg_depth_usd"] = 0.0
    if max_lead_days is not None:
        g["max_days_to_close"] = float(max_lead_days)
    return g


def gate_history(conn, series: str):
    """`pit_gates.GateHistory` for one series — the db-state gates, sliceable per day.

    **Why this replaces the old "the db gates stay out" rule.** That rule cited
    walkforward.py's original objection: the stored calibration map / capture memory /
    skill ratio / conformal state were fit on data covering the evaluation window, so
    consulting them is self-referential. The objection was right about the STORED
    artefacts and wrong as a reason to drop the gates — `research/pit_gates` rebuilds each
    one at every simulated day from events that closed strictly earlier, which is exactly
    what the live path had. #109 applied that to `walkforward`; leaving `pnl_score` on the
    old rule meant **the parameter selector was ranking candidates on a strategy nobody
    runs** — skill BLOCK alone fires 54 times in the displayed 75-day window.

    **What is deliberately held fixed.** The history is built once per series from the
    INCUMBENT (registered-default) predictions, not rebuilt per candidate parameter set.
    That is not a shortcut: the gate state is a property of what the live system actually
    accumulated, and a candidate set that was never deployed did not produce a different
    calibration map or skill ratio in the real world. Rebuilding it per set would also be
    circular — the gates would be fit on the very predictions being scored — and would
    turn a 73-set grid into 73 full replays per series.

    Memoised per (connection, series) so `score_matrix`'s events x sets loop pays one
    build. That matters more than it sounds: `GateHistory.__init__` runs a full
    `backtest.replay_series` PLUS an `eval.decision_replay` for the series, so a 73-set
    grid over 11 events was paying 803 of them.

    **#145 — the cache used to live on the connection and never worked.** It was
    `conn._pnl_gate_hist = cache` inside a `try`, but `sqlite3.Connection` does not accept
    arbitrary attributes, so the `except AttributeError` fallback fired on every single
    call and quietly rebuilt. It was quiet because it is only a performance bug: the answer
    was always correct, just recomputed. The key here is `(id(conn), series)` and the
    connection object itself is held in the value, which is what makes the id safe — an
    id can only be recycled after its object is freed, and this reference prevents that.
    """
    key = (id(conn), series)
    hit = _GATE_HIST.get(key)
    if hit is not None:
        return hit[1]
    hist = _build_gate_history(conn, series)
    _GATE_HIST[key] = (conn, hist)
    return hist


def _build_gate_history(conn, series: str):
    from prediction_market_macro.research.pit_gates import GateHistory
    return GateHistory(conn, series)


def _candle_quote(conn, ticker: str, asof: datetime):
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    if r is None:
        return None, None
    return r["yes_bid_close"], r["yes_ask_close"]


def quotable_events(conn, series: str, before: datetime | None = None) -> list[dict]:
    """Settled events of `series` with at least one leg priced inside their entry window.

    The Brier path's `scored_universe` needs only a settled outcome and so reaches back to
    2022; this one needs a price to trade against and so cannot. Both orderings are by
    close time, oldest first, so the two universes align event-for-event where they
    overlap and the proxy check in `param_wf` can pair them.

    The candle requirement is enforced HERE rather than left to `event_pnl` returning
    None. Both give the same answer, but leaving it implicit meant every caller replayed
    the full 7-day window over events that could never produce a trade — on KXAAAGASW
    that was 70 events scanned to score 11, and with a 73-set grid the difference is an
    hour. `end_ts > 100000` excludes the 404 sentinel rows: `kalshi_md.candles` writes an
    `end_ts=0` row when Kalshi has no candlesticks for a ticker, so a plain EXISTS on
    `candles` would count 6696 legs that carry no price at all.
    """
    rows = conn.execute(
        "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
        " AND s.result IN ('yes','no')"
        " AND EXISTS(SELECT 1 FROM candles k WHERE k.ticker=s.ticker"
        "            AND k.end_ts > 100000"
        "            AND k.end_ts <= strftime('%s', c.close_time))"
        " GROUP BY s.period ORDER BY ct",
        (series,)).fetchall()
    out = []
    for r in rows:
        if not r["ct"]:
            continue
        close_ts = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
        if before is not None and close_ts >= before:
            continue
        out.append({"series": series, "tok": r["period"], "close_ts": close_ts})
    return out


def entry_days(close_ts: datetime, gates: dict) -> list[datetime]:
    """The simulated days on which this event is a live candidate, earliest first.

    Same 16:00 UTC stamp `walkforward.run` puts on every simulated day, so an event
    replayed here sees the identical asof — and therefore the identical candle — it would
    have seen inside the full-window simulation. A day only counts while the market is
    still open (`day < close_ts`); the `max_days_to_close` gate bounds the other end, and
    is read from `gates` rather than hardcoded so a lead sweep moves both together.
    """
    lead = float(gates.get("max_days_to_close", 7.0))
    first = close_ts - timedelta(days=lead)
    d = first.replace(hour=OFFSET_HOUR, minute=0, second=0, microsecond=0)
    if d < first:                      # the 16:00 stamp of that date is already too early
        d += timedelta(days=1)
    out = []
    while d < close_ts:
        out.append(d)
        d += timedelta(days=1)
    return out


def _legs_at(conn, series: str, tok: str, day: datetime):
    """(meta, results) exactly as walkforward builds them: legs quotable at `day`.

    Depth is handed in as effectively infinite because a daily candle does not carry a
    book; `wf_gates` waives the depth gate to match, so the two together neither invent
    liquidity nor block on its absence.
    """
    meta, results = [], {}
    for l in conn.execute(
            "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type,"
            " c.close_time, s.result FROM contracts c"
            " JOIN settlements s ON s.ticker=c.ticker"
            " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
            (series, tok)).fetchall():
        b, a = _candle_quote(conn, l["ticker"], day)
        if b is None and a is None:
            continue
        meta.append({"ticker": l["ticker"], "strike": l["floor_strike"],
                     "cap_strike": l["cap_strike"],
                     "strike_type": l["strike_type"], "close_time": l["close_time"],
                     "yes_bid": b, "yes_ask": a, "bid_depth": 1e9, "ask_depth": 1e9})
        results[l["ticker"]] = l["result"]
    return meta, results


def _settle(st, count: int, results: dict) -> float | None:
    """Realised dollars on a settled structure, net of the taker fee on every leg."""
    realized = 0.0
    for leg in st.legs:
        res = results.get(leg.ticker)
        if res is None:
            return None
        realized += ((1.0 if res == leg.side else 0.0) - leg.price) * count \
            - taker_fee(leg.price, count)
    return realized


def _staked(st, count: int) -> float:
    """Capital at risk on production's definition — the same one `decide` books.

    `decide` records `size_usd = count * fill_cost(count)`, and `fill_cost` returns the NET
    debit on a bucket (`sum(px) - 1`), because one of the two legs pays in every branch.
    Summing raw leg prices instead charges a bucket its GROSS notional, ~$1 per contract too
    much. That leaves realised dollars untouched but inflates the denominator, so any ROI
    computed here reads better than the strategy is — and this is the objective the parameter
    selector ranks candidates by.

    This is the same defect fixed in `walkforward._trade_row` on 2026-08-05, which moved the
    displayed 75-day hybrid ROI from -9.17% to -13.21%; the two were written from the same
    wrong mental model and have to be fixed together. Singles are unaffected, which is why it
    hid: only the bucket structures (KXWTIW / natgas / claims `between` legs) differ.

    2026-08-11 (user display convention): entry taker fees are part of the stake — the
    full cash a losing position forfeits — so ROI bottoms out at -100% exactly.
    `event_pnl`'s realized nets the same fees; numerator and denominator match.
    """
    return st.fill_cost(count) * count + sum(taker_fee(l.price, count)
                                             for l in st.legs)


def event_pnl(conn, series: str, tok: str, key: str, close_ts: datetime,
              params: dict | None = None, gates: dict | None = None,
              bankroll: float = BANKROLL, db_gates: bool = True,
              model_exits: bool = True) -> dict | None:
    """Replay one event's entry window under one parameter set. None if unscoreable.

    Returns {"edge","argmax","hybrid","staked",...} in dollars. `hybrid` is the live rule
    — the edge trade where it fired, the favourite where the edge stream passed — and is
    the number this module exists to produce; `edge` and `argmax` come back alongside it
    because a parameter set that improves the total by helping one stream and hurting the
    other is worth being able to see.

    A parameter set that never opens anything scores 0.0 rather than None. That is not a
    missing observation: declining to bet is an outcome the strategy can have, it is what
    the incumbent does on most events, and dropping those rows would score every candidate
    only on the events where it happened to be brave.

    **This function must stay bit-identical to `walkforward.run`'s inner loop.** They are
    two replays of one strategy and they diverged once already: this one ran with the
    db-state gates off and `market_implied=None`, so the parameter selector was ranking
    candidates on a strategy production does not run. `tests/test_pnl_score_matches_
    walkforward.py` pins the two together on (desc, count, staked, realized).

    The ONE difference that cannot be removed, and is not a defect: `walkforward` applies
    `_sim_risk_veto` against the whole simulated book, and this function scores a single
    event in isolation, so it has no book to check. Parameter selection is a per-event
    comparison — every candidate would face the identical portfolio constraint anyway, so
    the veto cannot reorder them. `db_gates=False` restores the old ungated behaviour for
    diagnosis only.

    **`model_exits` (#144) closes the second divergence.** #142 put `ops/exits.py`'s rules
    inside `walkforward`; this module kept holding every position to settlement, so the
    daily DSR selector was again ranking candidates on a strategy neither the harness nor
    the live book runs — the same disease as #125/#133 through a different door, and it
    matters exactly as much: half of all live positions are closed by rule 1, so on those
    trades the settlement outcome is not the PnL. The rules themselves are IMPORTED from
    `walkforward` rather than copied. A third implementation of `hold_edge` is precisely
    what #141 cost, and this is the third site that would have needed one.
    """
    g0 = gates or wf_gates()
    disp = SERIES_DISPATCH[series]
    fn = getattr(importlib.import_module(disp[0]), disp[1])
    spec = REGISTRY[series]
    hist = gate_history(conn, series) if db_gates else None
    edge_t = argmax_t = None
    seen_any = False
    # #144 — the held path's exit check, in `walkforward._trade_row`'s own terms.
    _pmf_cache: dict[str, dict | None] = {}

    def _pmf_for(d: datetime):
        """The candidate model re-run as of a later held day, in `exits.run`'s shape.

        The parameters are the CANDIDATE set being scored, held fixed across the hold —
        which is the honest counterfactual here. `walkforward` asks its PIT daily selector
        again at `d` because it is simulating the selector; this function is scoring one
        fixed set, so re-asking the selector would price the hold under a different set
        than the entry and the comparison between candidates would stop being paired.

        RAW model output, never a pooled blend: `predict_all` writes the model's own pmf
        to `preds` and that is the row the live `exits.run` reads.
        """
        ck = d.date().isoformat()
        if ck in _pmf_cache:
            return _pmf_cache[ck]
        out_m: dict | None = None
        try:
            p = fn(conn, d, key, series=series, params=params)
            out_m = ({"probs": dict(p.dist.probs)} if isinstance(p.dist, Categorical)
                     else {"pmf": grid_pmf(p.dist, spec.round_rule)})
        except Exception:                                        # noqa: BLE001
            out_m = None
        _pmf_cache[ck] = out_m
        return out_m

    def _exit_or_settle(st, count: int, entry_day: datetime, leg_meta: dict,
                        g_eff: dict, results: dict) -> tuple[float | None, dict | None]:
        """(realised dollars, exit row) — the exit price when a rule fires, else settlement.

        Mirrors `walkforward._trade_row`: `ex["mtm"]` already nets the entry fee, the exit
        fee and `exits.SLIP`, which is the arithmetic `ops/exits._write_exit` books live,
        so it REPLACES the settlement figure rather than adjusting it.
        """
        realized = _settle(st, count, results)
        if realized is None or not model_exits:
            return realized, None
        from prediction_market_macro.research.walkforward import _first_exit, _mtm_path
        path = _mtm_path(conn, st, count, entry_day, close_ts, OFFSET_HOUR,
                         pmf_for=_pmf_for, leg_meta=leg_meta)
        ex = _first_exit(st, path, g_eff.get("min_leg_price", 0.10))
        return (realized if ex is None else ex["mtm"]), ex
    for day in entry_days(close_ts, g0):
        if edge_t is not None and argmax_t is not None:
            break
        meta, results = _legs_at(conn, series, tok, day)
        if not meta:
            continue
        # skill BLOCK comes FIRST and aborts the whole event, argmax leg included —
        # `decide_all` `continue`s here, and a hybrid that keeps buying favourites on a
        # blocked series is not the live rule (walkforward.py:318-324).
        gs = hist.asof(day) if hist is not None else None
        if gs is not None and gs.blocked is not None:
            continue
        # §25.4 per-series switch, same order and same consequence as in `decide_all`.
        # Note what this means for the selector: on a disabled series EVERY candidate
        # param set scores zero trades and they tie. That is the honest answer, not a
        # defect — the state is built from the incumbent's history (see `gate_history`),
        # so no candidate gets to argue its way back on, exactly as no candidate gets to
        # rebuild the skill ratio in its own favour.
        if gs is not None and gs.disabled is not None:
            continue
        try:
            pred = fn(conn, day, key, series=series, params=params)
        except Exception:                                        # noqa: BLE001
            continue
        seen_any = True
        market_fairs, wide_book, model_mean = None, False, None
        if isinstance(pred.dist, Categorical):
            probs = dict(pred.dist.probs)
            structs = _structs_categorical(meta, probs)
            pv = [v for v in probs.values() if v > 0]
        else:
            pmf = grid_pmf(pred.dist, spec.round_rule)
            structs = enumerate_structs(meta, pmf, strict=strict_gt_at(spec.ticker, day))
            pv = [v for v in pmf.values() if v > 0]
            model_mean = sum(k * v for k, v in pmf.items())
            if db_gates:
                # devig the same book production does, so decide()'s sanity gate compares
                # against the DEVIGGED market prob rather than the raw ask it is about to
                # pay. Leaving this None is not "no gate", it is a silently looser one.
                from prediction_market_macro.model.ensemble import (WIDE_SPREAD,
                                                                    median_spread)
                from prediction_market_macro.research.walkforward import _implied
                _im = _implied(meta)
                wide_book = (median_spread(meta) or 0.0) > WIDE_SPREAD
                if _im.get("pmf"):
                    market_fairs = {ms.desc: ms.fair for ms in enumerate_structs(
                        meta, {float(k): v for k, v in _im["pmf"].items()},
                        strict=strict_gt_at(spec.ticker, day))}
        entropy_norm = (-sum(p * math.log(p) for p in pv) / math.log(len(pv))
                        if len(pv) > 1 else None)

        # ── the remaining db-state gates, in decide_all's order ──────────────────
        g = dict(g0)
        if gs is not None:
            structs = gs.calibrate_structs(structs)
            structs, _drops = gs.filter_capture(
                structs, model_mean, spec.round_rule,
                {m["ticker"]: m["strike"] for m in meta})
            if not structs:
                continue
            fl = conn.execute(
                "SELECT MAX(severity) sev FROM event_flags WHERE family=?"
                " AND ts<=? AND expires_ts>?",
                (spec.family, day.isoformat(), day.isoformat())).fetchone()
            if fl and fl["sev"]:
                g["max_size_usd"] *= 0.5
                g["max_entropy_norm"] = g["max_entropy_norm"] - 0.02 * fl["sev"]
            g = gs.apply_gates(g)

        # #130: a wide book TIGHTENS the sanity gate, it does not switch it off. Applied
        # here, after calibration, because calibration moves `st.fair` and that is what
        # "further from our fair" is measured against.
        if wide_book:
            _mf = market_fairs or {}
            market_fairs = {st.desc: max(_mf.get(st.desc, st.cost), st.cost,
                                         key=lambda p, f=st.fair: abs(f - p))
                            for st in structs}

        # #142/#144 — what a held position's exit check needs on a LATER day: this event's
        # strike metadata (fixed per ticker), plus `_pmf_for`'s re-run of the model.
        leg_meta = {m["ticker"]: {"strike": m["strike"], "cap_strike": m["cap_strike"],
                                  "strike_type": m["strike_type"]} for m in meta}

        # ── stream 1: EDGE — the gated production decision ──
        if edge_t is None:
            dec = decide(structs, now=day, close_time=close_ts, release_ts=None,
                         market_implied=market_fairs, already_open=False,
                         bankroll=bankroll, gates=g, entropy_norm=entropy_norm)
            if dec.action == "open" and dec.struct is not None:
                r, ex = _exit_or_settle(dec.struct, dec.count, day, leg_meta, g, results)
                if r is not None:
                    edge_t = {"realized": r, "staked": _staked(dec.struct, dec.count),
                              "day": day.date().isoformat(), "desc": dec.struct.desc,
                              "fair": dec.struct.fair, "cost": dec.struct.cost,
                              "exit_rule": None if ex is None else ex["rule"],
                              "exit_day": None if ex is None else ex["day"]}
        # ── stream 2: ARGMAX (favourite), verbatim from walkforward.py:502-542 ──
        if argmax_t is None:
            # decide_all gates this leg on 0.03 <= dtc <= max_days_to_close; the lower
            # bound (~43 min) keeps it out of the last minutes before close.
            _dtc = (close_ts - day).total_seconds() / 86400.0
            inside = 0.03 <= _dtc <= g.get("max_days_to_close", 7.0)
            cands = [st for st in structs if 0.10 <= st.cost <= 0.90 and st.fair > 0.5]
            if inside and cands:
                st_a = max(cands, key=lambda x: x.fair)
                # select-THEN-filter: defer to the market when it is the more confident
                # forecaster (fair <= cost); fair > cost is adverse selection
                if st_a.fair > st_a.cost:
                    st_a = None
                if st_a is not None:
                    # size against the FILL, not the quote — `_place_argmax` re-sizes once
                    # through fill_cost, and skipping that pass overstates the count (and
                    # so the stake) on every cheap leg
                    count_a = max(1, int(1.0 / st_a.cost))
                    count_a = max(1, int(1.0 / max(st_a.fill_cost(count_a), 0.01)))
                    r, ex = _exit_or_settle(st_a, count_a, day, leg_meta, g, results)
                    if r is not None:
                        argmax_t = {"realized": r, "staked": _staked(st_a, count_a),
                                    "day": day.date().isoformat(), "desc": st_a.desc,
                                    "fair": st_a.fair, "cost": st_a.cost,
                                    "exit_rule": None if ex is None else ex["rule"],
                                    "exit_day": None if ex is None else ex["day"]}
    if not seen_any:
        return None
    live = edge_t if edge_t is not None else argmax_t
    return {"series": series, "period": key,
            "close": close_ts.date().isoformat(),
            "edge": edge_t["realized"] if edge_t else 0.0,
            "argmax": argmax_t["realized"] if argmax_t else 0.0,
            "hybrid": live["realized"] if live else 0.0,
            "staked": live["staked"] if live else 0.0,
            "traded": live is not None,
            "stream": ("edge" if edge_t else "argmax" if argmax_t else "none"),
            "trade": live}


def score_matrix(conn, series: str, grid: list[dict], universe: list[dict],
                 log=None) -> tuple[list[dict], list[list[float]], list[list[dict]]]:
    """(kept_events, [[hybrid PnL per set] per event], [[detail per set] per event]).

    An event is kept only when EVERY set in the grid can be replayed on it, mirroring
    `param_wf.score_matrix`: a partial row would compare candidates on different samples,
    which is precisely the bias the paired test in `dsr` is there to remove.
    """
    kept, mat, det = [], [], []
    for ev in universe:
        row, rows = [], []
        for p in grid:
            r = event_pnl(conn, series, ev["tok"], ev["key"], ev["close_ts"],
                          params=(p or None))
            if r is None:
                row = None
                break
            row.append(r["hybrid"])
            rows.append(r)
        if row is None:
            continue
        kept.append(ev)
        mat.append(row)
        det.append(rows)
    if log:
        log(f"  {series}: PnL-scored {len(kept)}/{len(universe)} events x {len(grid)} sets")
    return kept, mat, det
