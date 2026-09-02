"""research/walkforward.py — daily-granularity walk-forward simulation (user ask
2026-07-31): run the CURRENT system as if it had been live for the past N days,
advancing one day at a time under strict PIT.

Each simulated day D at 16:00 UTC (~noon ET):
  * every (series, period) whose market was OPEN at D and has since SETTLED is a
    candidate (settled-only so every position can be scored to the end)
  * the book is rebuilt from stored daily candles as of D (depth unknown → waived)
  * the PRODUCTION model predicts at asof=D (its internal fits — AAA drift
    regression, first prints, GDPNow vintages — all read knowledge_time <= D)
  * the PRODUCTION decide() runs with the SAME gates the live path runs, db-state
    gates included. Those were excluded originally because the STORED artefacts
    (calibration map, capture memory, skill ratio, conformal state) are fit on
    data covering this window. The answer is not to drop the gates — that left the
    backtest measuring a strategy production does not run, which is worse — but to
    rebuild each one at every simulated day from events that closed strictly
    earlier. `research/pit_gates` does that; `--pure-gates` restores the old
    behaviour for diagnosis.
  * model parameters come from the SAME daily DSR-gated selector production uses
    (`research/param_select`), re-evaluated as the trailing sample grows, rather
    than being pinned to the registered defaults. `--fixed-params` pins them.
  * one open per (series, period) for the whole run (first day the gates clear —
    exactly the live no-averaging rule); positions settle at their real results

Outputs: day-by-day equity curve, win rate, per-series PnL, entry-lead-time
buckets (how many days before settlement the entry happened — the diagnostic for
"are early entries the losers?"). Stored as experiments row 'daily_walkforward'.

Discipline: this is an EVALUATION harness. Improve → rerun is allowed for
STRUCTURAL fixes only; never tune thresholds until this window looks good
(overfit guarantee). Hold out the trailing third when judging any change.

    python -m prediction_market_macro.research.walkforward [--days 30]
"""
from __future__ import annotations

import argparse
import importlib
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from prediction_market_macro.config.registry import REGISTRY, effective_strike_type, strict_gt_at
from prediction_market_macro.model.common import Categorical, grid_pmf
from prediction_market_macro.ops.decide_all import _structs_categorical
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH
from prediction_market_macro.strategy.decision import GATES, decide
from prediction_market_macro.strategy.edge import enumerate_structs, taker_fee
from prediction_market_macro.util.periods import kalshi_period_to_key


class _MLBook:
    """The `research/selector.py` learned bet-selector, refitted per SIMULATED DAY.

    Why it moved here (2026-08-20, user instruction). `selector.py` runs its own
    harness: no db_gates, no pit_gates, no exits, flat $1, one bet per event. Its
    output was nevertheless tabled in the Walk-Forward Lab beside `argmax`, which
    carries every one of those, and read +104.9% against argmax's +17.5% on the same
    30 days. That comparison is not close to fair — it is mostly a measurement of
    which harness is more permissive. Serving the same model inside THIS loop puts it
    behind the identical gate chain, exit rules and risk vetoes, which is the only way
    "is the learned line better?" becomes a question about the model.

    PIT contract, unchanged from selector's: the model that scores an event on
    simulated day D is fit ONLY on structures whose event CLOSED strictly before D.
    Refit is cached per day rather than per event — the training set is a function of
    the day alone, so a per-event refit would be the same fit computed ~10x.

    One train/serve gap is real and is NOT hidden: `build_dataset` labels rows from
    RAW structs, while this loop scores structs after `gs.calibrate_structs` and
    `gs.filter_capture`. Calibration is identity-pinned in production (README §标定
    设计研究: identity beat all four candidates), so the gap is normally nil, and
    `gate_stats['calibrated_events']` is the number that says whether it stayed nil on
    any given run. Capture only ever REMOVES candidates, which cannot move a fitted
    coefficient — it can only shrink the menu this scorer chooses from, which is
    exactly what production does to its own menu.
    """

    def __init__(self, conn, max_events: int = 400):
        from prediction_market_macro.research.selector import build_dataset
        self.data = build_dataset(conn, max_events)
        self._fits: dict[str, object] = {}

    def fit_for(self, day: datetime):
        """(scaler, clf) trained on everything settled before `day`, or None."""
        from prediction_market_macro.research.selector import MIN_TRAIN
        ck = day.date().isoformat()
        if ck in self._fits:
            return self._fits[ck]
        train = [r for r in self.data if r["close"].date().isoformat() < ck]
        out = None
        if len(train) >= MIN_TRAIN and len({r["y"] for r in train}) >= 2:
            from sklearn.linear_model import LogisticRegression
            from sklearn.preprocessing import StandardScaler
            Xt = np.array([r["x"] for r in train])
            yt = np.array([r["y"] for r in train])
            # per-event weights: an event's structures are ONE correlated observation,
            # so an event quoting 14 legs must not outvote one quoting 5. Same reason
            # and same arithmetic as selector.walkforward_eval — copied deliberately
            # rather than re-derived, because a second opinion about the weighting
            # would make the two lines incomparable for a reason unrelated to gates.
            n_ev: dict[tuple, int] = {}
            for r in train:
                n_ev[(r["series"], r["period"])] = \
                    n_ev.get((r["series"], r["period"]), 0) + 1
            wt = np.array([1.0 / n_ev[(r["series"], r["period"])] for r in train])
            sc = StandardScaler().fit(Xt)
            clf = LogisticRegression(max_iter=500)
            clf.fit(sc.transform(Xt), yt, sample_weight=wt)
            out = (sc, clf)
        self._fits[ck] = out
        return out

    @staticmethod
    def features(st, max_fair: float, spread_med: float | None,
                 entropy: float | None, lead_days: float, family: str) -> list[float]:
        """selector._event_rows' `x`, in the same order. The order IS the contract —
        a column added on one side and not the other silently reassigns every
        coefficient, so both live in one place and this is the copy that must match."""
        from prediction_market_macro.research.selector import FAMS
        kind_yes = 1.0 if (st.kind == "single" and st.legs[0].side == "yes") else 0.0
        kind_no = 1.0 if (st.kind == "single" and st.legs[0].side == "no") else 0.0
        return [st.fair, st.cost, st.fair - st.cost, abs(st.fair - st.cost),
                1.0 if st.fair >= max_fair - 1e-9 else 0.0,
                1.0 if st.fair <= st.cost else 0.0,
                # selector's own fallbacks when the book gives no spread / no entropy
                0.05 if spread_med is None else spread_med,
                0.5 if entropy is None else entropy,
                lead_days / 7.0, kind_yes, kind_no] + \
               [1.0 if family == f else 0.0 for f in FAMS]


def _arb_scan(meta: list[dict], violations: list[dict]) -> dict:
    """Price every detected violation exactly as `strategy/arb.execute` prices it.

    The census the backtest never had. `arb` is wired live and has opened zero
    positions; without this the backtest simply had no arb line, so "zero" and "not
    modelled" were the same blank. They are not the same fact, and only one of them
    is evidence.

    Returns the best net-per-contract after the ceil-to-cent per-leg taker fee, and
    whether it would clear MIN_NET. **Does not size.** Candle rows carry no depth
    (`bid_depth`/`ask_depth` are 1e9 here), so 铁律 5 cannot bind in simulation — a
    sized backtest arb would be a fiction about liquidity, and reporting a count with
    no dollars is the honest shape for a line whose live twin is depth-limited.
    """
    from prediction_market_macro.strategy.arb import MIN_NET
    by_ticker = {m["ticker"]: m for m in meta}
    best = None
    # violations that never reached the fee gate at all. Same three drop paths
    # `arb.execute` has, and counted for the same reason it now alerts on them: a
    # census reading "4 violations, best net None" is unreadable — it cannot say
    # whether the fee gate held or the structure was never priceable in the first
    # place, and those are opposite conclusions about whether the money was real.
    dropped = 0
    for v in violations:
        if "buy" in v:
            lo, hi = v["buy"], v["sell"]
            if lo["ticker"] not in by_ticker or hi["ticker"] not in by_ticker:
                dropped += 1
                continue
            prices = [lo["yes_ask"], round(1 - hi["yes_bid"], 4)]
            payoff_min = 1.0
        elif v.get("kind") == "buy_all_yes":
            prices = [m["yes_ask"] for m in meta if m.get("yes_ask") is not None]
            payoff_min = 1.0
        elif v.get("kind") == "buy_all_no":
            prices = [round(1 - m["yes_bid"], 4) for m in meta
                      if m.get("yes_bid") is not None]
            payoff_min = float(len(prices) - 1)
        else:
            dropped += 1
            continue
        if not prices or any(p is None or not (0 < p < 1) for p in prices):
            dropped += 1
            continue
        cost = sum(prices)
        fees = sum(taker_fee(p, 1) for p in prices)
        net = payoff_min - cost - fees
        if best is None or net > best:
            best = net
    return {"n_violations": len(violations), "best_net": best, "dropped": dropped,
            "clears": best is not None and best >= MIN_NET}


def _implied(meta: list[dict]) -> dict:
    """The devigged market view of a book, choosing the SAME devig `decide_all` does.

    `decide_all:226` picks `bucket_implied` the moment any leg is a `between`, because a
    bucket book is a partition and its leg prices normalise multiplicatively, whereas
    `ladder_implied` reads the same prices as a survival curve and DIFFERENCES them. Fed a
    partition that curve is not monotone, so the isotonic fit flattens it and most of the
    differenced mass lands in one bucket — a market pmf that is not wrong at the margin but
    unrelated to the book. That feeds `decide()`'s sanity gate (and, in pooled mode, the
    pool itself), so getting it wrong makes the backtest gate on a fiction.

    Only KXWTIW quotes `between` legs today, which is why this went unnoticed: every other
    series is a `greater` ladder where the two devigs agree by construction.
    """
    from prediction_market_macro.strategy import devig as _dv
    legs = [{"strike": m["strike"], "yes_bid": m["yes_bid"], "yes_ask": m["yes_ask"],
             "ticker": m["ticker"], "strike_type": m.get("strike_type")} for m in meta]
    return (_dv.bucket_implied(legs)
            if any(l.get("strike_type") == "between" for l in legs)
            else _dv.ladder_implied(legs))


def _candle_quote(conn, ticker: str, asof: datetime):
    r = conn.execute(
        "SELECT yes_bid_close, yes_ask_close FROM candles WHERE ticker=? AND end_ts<=?"
        " ORDER BY end_ts DESC LIMIT 1", (ticker, int(asof.timestamp()))).fetchone()
    if r is None:
        return None, None
    return r["yes_bid_close"], r["yes_ask_close"]


def _struct_mid(st, quotes: dict) -> float | None:
    """The MARKET's implied probability that `st` pays, on `Struct.fill_cost`'s scale.

    `fill_cost` quotes a bucket as `sum(side prices) - 1`, because YES(>lo) + NO(>hi) has
    one leg paying in every branch — so the package is a plain binary whose price is that
    difference. Applying the same subtraction to MIDS gives the market's probability, and
    that is what makes `y - m` a forecast error PR-7 can average.

    Returns None on a ONE-SIDED book. `enumerate_structs` needs only `yes_ask` to build a
    yes leg and only `yes_bid` to build a no leg, so a struct can exist with no mid at
    all; substituting the touch would manufacture exactly the drawdown PR-7 is testing
    for, so those rows are dropped instead.
    """
    total = 0.0
    for leg in st.legs:
        m = quotes.get(leg.ticker) or {}
        b, a = m.get("yes_bid"), m.get("yes_ask")
        if b is None or a is None:
            return None
        mid = (b + a) / 2.0
        total += mid if leg.side == "yes" else 1.0 - mid
    return total - (1.0 if st.kind == "bucket" else 0.0)


def _hold_edge(pmf: dict | None, st, quotes: dict) -> float | None:
    """`ops/exits.py`'s rule-1 metric, computed on a walk-forward day.

    `pmf` is `{"pmf": ladder}` or `{"probs": categorical}` — the same two shapes
    `exits.run` reads out of the `preds` row, in the same priority order.

    Deliberately a re-implementation of ten lines rather than a call into `exits.run`:
    that function is a DB-mutating loop over `open_positions` reading `quotes`/`preds`
    tables the simulation does not write. What must match is the METRIC, and this is the
    same one — per leg, `fair(side) - mid(side)`, SUMMED over the structure (#141), with
    the same `two_sided` refusal to price a leg nobody is making a market in.

    Returns None when the edge is not measurable, which is `exits.run`'s "hold": a
    missing quote, a wide book, or a strike shape `leg_fair` cannot price.
    """
    from prediction_market_macro.model.common import leg_fair
    from prediction_market_macro.strategy.edge import two_sided
    probs = pmf.get("probs") if isinstance(pmf, dict) and "probs" in pmf else None
    ladder = pmf.get("pmf") if isinstance(pmf, dict) and "pmf" in pmf else None
    if probs is None and ladder is None:
        return None
    total = 0.0
    for leg in st.legs:
        m = quotes.get(leg.ticker)
        if m is None:
            return None
        b, a = m["yes_bid"], m["yes_ask"]
        if not two_sided(b, a):
            return None
        if probs is not None:                       # categorical leg (Fed), as exits.py
            fair_yes = float(probs.get(leg.ticker.rsplit("-", 1)[-1], 0.0))
        else:
            if m.get("strike") is None and m.get("cap_strike") is None:
                return None
            try:
                fair_yes = leg_fair(ladder, m.get("strike_type") or "greater",
                                    m.get("strike"), m.get("cap_strike"))
            except Exception:                                    # noqa: BLE001
                return None
        fair_side = fair_yes if leg.side == "yes" else 1.0 - fair_yes
        mid = (b + a) / 2.0
        total += fair_side - (mid if leg.side == "yes" else 1.0 - mid)
    return total


def _mtm_path(conn, st, count: int, entry_day: datetime, close_ts: datetime,
              offset_hour: int = 16, pmf_for=None, leg_meta: dict | None = None
              ) -> list[dict]:
    """§25.5 — what the position was worth, day by day, between entry and settlement.

    Answers one question and only one: **was this bet ever green before it settled, and
    by how much.** The backtest has never had a held path — `_settle_struct` jumps
    straight from entry to the 0/1 outcome — so "we were up and gave it back" has been
    unanswerable rather than answered no.

    `mtm` is the LIQUIDATION value, priced the way `ops/exits.py` actually exits: sell a
    yes leg into the bid, close a no leg against the ask, minus `exits.SLIP`, minus the
    exit taker fee, minus the entry fee already paid. `mtm_mid` is the same thing priced
    at the midpoint. Both are reported because the gap between them IS the answer to
    "could we have taken it": on this book a mid-based path routinely shows a profit that
    no bid would have paid. A midpoint is not a price on a two-sided-but-wide book — the
    same reasoning that made `exits.py` refuse to act on mids at all (KXCPIYOY-26SEP-T3.4
    quoting 0.18/0.98 has a "mid" of 0.58 that nobody will trade).

    The `mtm_peak` READING off this path is a hindsight oracle and must not become an
    exit rule without pre-registration: daily candle closes are one observation a day, a
    peak visible here was reachable only if we happened to look at that moment, and
    picking the threshold that maximises this window's PnL is the overfit §25.1 exists to
    prevent. `hold_edge` is different in kind — it is not read off the outcome, it is the
    metric the LIVE `ops/exits.py` already computes every cycle, and #142 exists because
    the backtest was not computing it at all.

    When `pmf_for` is supplied the path also carries `hold_edge`, the model's structure
    edge against that day's midpoint (see `_hold_edge`). `pmf_for(d)` must re-predict at
    asof=d with the parameters selected for d — anything cached from entry day would make
    the exit rule clairvoyant in the opposite direction (stale, not leaky, but wrong).
    """
    from prediction_market_macro.ops.exits import SLIP
    out: list[dict] = []
    d = entry_day + timedelta(days=1)
    while d < close_ts:
        mtm = mtm_mid = 0.0
        ok = True
        quotes: dict[str, dict] = {}
        for leg in st.legs:
            b, a = _candle_quote(conn, leg.ticker, d)
            if b is None or a is None:
                ok = False
                break
            quotes[leg.ticker] = {**(leg_meta or {}).get(leg.ticker, {}),
                                  "yes_bid": b, "yes_ask": a}
            exit_px = b if leg.side == "yes" else 1.0 - a
            mid = (b + a) / 2.0
            mid_px = mid if leg.side == "yes" else 1.0 - mid
            exit_px = max(exit_px - SLIP, 0.01)
            mtm += ((exit_px - leg.price) * count - taker_fee(leg.price, count)
                    - taker_fee(exit_px, count))
            mtm_mid += ((mid_px - leg.price) * count - taker_fee(leg.price, count)
                        - taker_fee(mid_px, count))
        if ok:
            # PR-7's `m_mid` (see `_struct_mid`). Recorded rather than derived later
            # because `mtm_mid` cannot be inverted back to it — `taker_fee` is nonlinear
            # in the leg price, so the fee term is not recoverable from the sum.
            mkt = _struct_mid(st, quotes)
            row = {"day": d.date().isoformat(), "mtm": round(mtm, 4),
                   "mtm_mid": round(mtm_mid, 4),
                   "m_mid": None if mkt is None else round(mkt, 6)}
            if pmf_for is not None:
                pmf = pmf_for(d)
                he = None if pmf is None else _hold_edge(pmf, st, quotes)
                row["hold_edge"] = None if he is None else round(he, 6)
                # the exit-side price the rule would actually get, and the depth test
                # `exits.run` applies (candle depth is unknown, so that leg of the test
                # is waived here exactly as it is waived at entry)
                row["exit_px_ok"] = all(
                    max((quotes[l.ticker]["yes_bid"] if l.side == "yes"
                         else 1.0 - quotes[l.ticker]["yes_ask"]) - SLIP, 0.01) >= 0.02
                    for l in st.legs)
            out.append(row)
        d += timedelta(days=1)
    return out


def _first_exit(st, path: list[dict], min_leg_price: float) -> dict | None:
    """The first day `ops/exits.py` would have closed this position, or None.

    Ports rules 1 and 3 verbatim; rule 2 (red-light forced exit on
    `risk.breaker_tripped`) is NOT ported. That used to rest on "52 of 52 live exits are
    rule 1 and rule 2 has never fired once", and it said that if rule 2 ever fired this
    comment was the thing to come back to. **It fired.** On 2026-08-12 a
    `health_red:replay_mismatch` breaker closed three positions at once, and as of
    2026-08-20 the live ledger reads 5 rule-1 exits and 3 rule-2 exits.

    The cost of the omission is now measurable and it is not small. All five settled live
    positions exited early; held to settlement they would have returned -$1.65, and the
    exits turned that into +$0.52 — early exit IS the live PnL, worth +$2.17. Rule 2's
    three exits account for +$1.30 of it, and the simulation cannot see any of that: it
    exits 5 of 35 positions on the 60d window against live's 8 of 8. That is most of why
    `research/live_replay` reads the replay losing where live wins.

    Still not ported, and the reason is unchanged rather than convenient: the breaker's
    PIT state (health colour, ledger-mismatch flag, rolling-20 drawdown) is not
    reconstructible from what this harness stores, so a simulated rule 2 would be a guess
    with a $1.30 thumb on the scale. Note also WHAT tripped it — `replay_mismatch` is a
    plumbing alarm, not a market view; it had no opinion about the CPI print that was
    about to go against those three legs. Crediting the strategy with that $1.30 would be
    crediting it with a coin flip that landed well. The honest reading is that the
    backtest understates live's exit discipline AND that live's headline overstates its
    own skill, and porting rule 2 would fix the first by baking in the second.

    Both live rules read only that day's model and that day's book, so scanning the path
    forward and taking the FIRST trigger is exactly the live sequence — `exits.run` gets
    one look per cycle and acts on the first one that qualifies.
    """
    from prediction_market_macro.ops.exits import EXIT_EDGE
    penny_entry = any(l.price < min_leg_price for l in st.legs)
    for row in path:
        he = row.get("hold_edge")
        if he is None:                       # unmeasurable => hold, as live
            continue
        # rule 3 — regime review: a penny-lottery entry today's gates would refuse,
        # with no current edge and still worth ≥2c/contract to unwind
        if penny_entry and he < 0.0 and row.get("exit_px_ok"):
            return {"day": row["day"], "hold_edge": he, "mtm": row["mtm"],
                    "rule": "regime_review"}
        # rule 1 — edge reversal
        if he < EXIT_EDGE:
            return {"day": row["day"], "hold_edge": he, "mtm": row["mtm"],
                    "rule": "edge_reversal"}
    return None


def _open_settled_events(conn, asof: datetime, now: datetime) -> list[dict]:
    """(series, period tok, close_ts) open at asof and settled by now."""
    rows = conn.execute(
        "SELECT s.series, s.period, MAX(c.close_time) ct FROM settlements s"
        " JOIN contracts c ON c.ticker=s.ticker"
        " WHERE s.result IN ('yes','no') GROUP BY s.series, s.period").fetchall()
    out = []
    for r in rows:
        if r["series"] not in SERIES_DISPATCH or not r["ct"]:
            continue
        close_ts = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
        if asof < close_ts <= now:
            out.append({"series": r["series"], "tok": r["period"],
                        "close_ts": close_ts})
    return out


class _GateBook:
    """Per-day db-state gates + per-day parameter selection, both PIT, both memoised.

    Kept out of `run` because both are expensive to build and cheap to slice: one pair of
    replays per series for the gates (see `pit_gates`), and one grid rescore per series
    per SETTLE for the parameters (the fingerprint cache in `param_select` is what makes
    the second affordable — without it this is a full grid replay on all 60 days).
    """

    def __init__(self, conn, db_gates: bool, select_params: bool, log=None,
                 param_override: dict[str, dict] | None = None,
                 select_mode: str = "dsr", argmin_start=None, argmin_end=None,
                 gate_params: bool = True):
        self.conn, self.db_gates, self.select_params, self.log = (conn, db_gates,
                                                                  select_params, log)
        # #199. Build the PIT gate history at the SAME per-day params this book hands the
        # prediction path, instead of at the registered defaults. Correctness, not a knob:
        # with it off, a run predicts at the selector's params while its skill ratio,
        # capture memory and conformal state describe a different configuration — the
        # "third combination" pit_gates.GateHistory's docstring names. False restores the
        # pre-#199 pairing for A/B only, the same status `model_exits=False` has.
        self.gate_params = gate_params
        # research knob (brute-force param sweep): {series: params} that takes
        # precedence over BOTH the daily selector and the registered defaults for
        # that series. Never set on a production-shaped run; the caller must carry
        # a distinguishing hash_tag or the experiments row would collide with the
        # canonical one (run() enforces the pairing).
        self.param_override = param_override or {}
        # select_mode='argmin' replays the 2026-08-11 production selector (daily raw
        # argmin over the trailing window, research/param_argmin) instead of the
        # DSR-gated one. PIT by the param_wf architecture: the PnL matrix is scored
        # once per series (each event at its own close-1h) and day-D selection is a
        # MASK over events that closed strictly before D — day D can never see its
        # own settle. The grid is probed on events before the sim window.
        self.select_mode = select_mode
        self.argmin_start, self.argmin_end = argmin_start, argmin_end
        self._amx: dict[str, tuple] = {}
        self._hist: dict[str, object] = {}
        self._params: dict[tuple[str, str], dict] = {}
        self.stats = {"skill_blocked": 0, "skill_defensive": 0, "capture_dropped": 0,
                      "calibrated_events": 0, "conformal_throttled": 0,
                      "risk_vetoed": 0, "params_adopted": 0, "param_rescores": 0,
                      # #202. Rescores AVOIDED by the sample-keyed cache. Reported rather
                      # than left implicit: `param_rescores` alone cannot distinguish "the
                      # selector was asked twice and the memo held" from "the cache is
                      # stale and the selector is no longer being asked at all".
                      "param_cache_hits": 0,
                      "series_disabled": 0,
                      # #155: the same verdict while the switch is off. Mutually exclusive
                      # with the line above — never both nonzero in one run.
                      "series_disabled_shadow": 0,
                      # §27.4 information gate, live-lane parity: KXAAAGASW quoted off the
                      # weekly GASREGW proxy is a state the live path refuses to trade.
                      "aaa_proxy_only": 0,
                      # #142 — the live exit rules, now modelled. rule 2 (health-red
                      # forced exit) is deliberately absent; see `_first_exit`.
                      "exit_edge_reversal": 0, "exit_regime_review": 0,
                      # #148 — favourites refused because rule 1 would have closed them
                      # on the entry cycle. Counted, not silent: this guard can in
                      # principle empty the argmax stream, and "the stream is quiet"
                      # must be distinguishable from "the stream is switched off".
                      "argmax_churn_blocked": 0,
                      # 2026-08-20 — the `ml` counterfactual's OWN risk vetoes, kept in a
                      # separate key from `risk_vetoed` so the live-lane number stays a
                      # statement about the live lane. It is measured against ml's own
                      # book (`_rows_of`), never the hybrid one.
                      "ml_risk_vetoed": 0}

    def gates_for(self, series: str, day: datetime):
        """`pit_gates.GateState` as of `day`, or None when db gates are off."""
        if not self.db_gates:
            return None
        h = self._hist.get(series)
        if h is None:
            from prediction_market_macro.research.pit_gates import GateHistory
            if self.log:
                self.log(f"  building PIT gate history: {series}")
            # #199. `params_for` is memoised on `param_select._sample_key`, so feeding it
            # one asof per graded event collapses to a handful of rescores per series
            # rather than one per event. MEASURED, because the first draft of this comment
            # stopped there and was wrong about what that costs: on KXJOBLESSCLAIMS the
            # 124 asofs do collapse to 15 rescores, but each one is a full grid replay at
            # ~8.8s, so the series went 3.0s -> 135.3s and the 30d run went 40s -> 6315s.
            # #202's sample-keyed cache is what makes that affordable on the second run.
            # The callable is passed even when the selector is off, where it returns None
            # for every asof and the history is bit-identical to the pre-#199 one — the
            # switch below is what turns the behaviour off, not an absent callable.
            at = ((lambda asof: self.params_for(series, asof))
                  if self.gate_params else None)
            h = self._hist[series] = GateHistory(self.conn, series, params_at=at)
        return h.asof(day)

    def params_for(self, series: str, day: datetime) -> dict | None:
        """Selected parameters as of `day`, or None for the registered defaults.

        Cached on `param_select._sample_key`, not on the day: the selector's answer
        cannot change until a new scoreable event settles OR a `manual_params` row is
        written, so a 60-day sim costs one rescore per such change rather than 60 per
        series.

        TWO caches, on the same key and for two different lifetimes. `self._params` is
        per-run and collapses the repeats within one sim; `param_selection_cache` (#202)
        is per-db and collapses them ACROSS runs, which is the one that matters once
        #199 started asking this question at every graded event's own asof. The weekly
        regen alone makes eight of these calls in one process — a sweep per lead, then
        60d, then 30d — and without the second cache each pays the full grid rescore.

        **The second clause is #198c and it was missing.** `_sample_key` used to be the
        sample fingerprint alone, on the reasoning that only a settle can move the
        answer. True of the DSR path, false of the override path: the `param_argmin`
        cron writes a row most weeks with no event settling, so the key held still and
        this cache served the pre-adoption answer across the adoption. Over the 75 days
        to 2026-08-27 that was 101 series-days on which the sim ran the registered
        defaults while production ran adopted params — including every simulated day of
        the live period. The stamp now lives in the key, so the invalidation is the
        selector's own concern rather than something this call site has to know about.
        """
        if series in self.param_override:      # research sweep: fixed for the whole run
            return self.param_override[series]
        if not self.select_params:
            return None
        if self.select_mode == "argmin":
            return self._argmin_params(series, day)
        from prediction_market_macro.research import param_select as _ps
        try:
            key = _ps._sample_key(self.conn, series, day)
        except Exception:                                        # noqa: BLE001
            return None
        hit = self._params.get((series, key))
        if hit is None:
            # #202. Before paying for a grid rescore, ask whether this exact sample has
            # already been scored — by an earlier run, or by the daily refresh. Measured
            # on the 30d A/B: #199 asks this question at 124 asofs per series instead of
            # ~30, and the extra ones land on days production never stood on, so the
            # DAY-keyed `param_selection` carry-forward cannot serve one of them. A miss
            # costs ~9s of grid rescore; the lookup costs one indexed SELECT.
            params = _ps.cached_answer(self.conn, series, key)
            if params is None:
                self.stats["param_rescores"] += 1
                try:
                    params, _rep = _ps.select_for(self.conn, series, day, log=None)
                except Exception as e:                           # noqa: BLE001
                    if self.log:
                        self.log(f"  param_select failed {series} {day.date()}: {e}")
                    params = {}
                else:
                    _ps.store_answer(self.conn, series, key, params, now=day)
            else:
                self.stats["param_cache_hits"] += 1
            if params:
                self.stats["params_adopted"] += 1
                if self.log:
                    self.log(f"  {series} {day.date()}: ADOPTED {params}")
            hit = self._params[(series, key)] = params
        return hit or None

    def _argmin_matrix(self, series: str):
        """(grid, kept_events, matrix) scored ONCE per series; day selection masks it."""
        hit = self._amx.get(series)
        if hit is not None:
            return hit
        from prediction_market_macro.research import param_argmin as _pa
        from prediction_market_macro.research import pnl_score as _psc
        from prediction_market_macro.util.periods import kalshi_period_to_key
        grid, _rep = _pa.build(self.conn, series, self.argmin_start)
        kept, mat = [], []
        if len(grid) > 1:
            uni = [{**e, "key": kalshi_period_to_key(e["tok"]), "close": e["close_ts"]}
                   for e in _psc.quotable_events(self.conn, series,
                                                 before=self.argmin_end)]
            uni = [e for e in uni if e["key"]]
            if uni:
                if self.log:
                    self.log(f"  argmin matrix: {series} {len(grid)} sets x "
                             f"{len(uni)} events")
                kept, mat, _det = _psc.score_matrix(self.conn, series, grid, uni)
        hit = self._amx[series] = (grid, kept, mat)
        return hit

    def _argmin_params(self, series: str, day: datetime) -> dict | None:
        from datetime import timedelta as _td
        from prediction_market_macro.research.param_argmin import WINDOW_DAYS
        grid, kept, mat = self._argmin_matrix(series)
        if len(grid) < 2 or not kept:
            return None
        lo = day - _td(days=WINDOW_DAYS)
        rows = [i for i, e in enumerate(kept) if lo <= e["close"] < day]
        if not rows:
            return None
        totals = [sum(mat[i][j] for i in rows) for j in range(len(grid))]
        best = max(range(len(grid)), key=lambda j: totals[j])
        return dict(grid[best]) if best else None


def _sim_risk_veto(open_rows: list[dict], series: str, period: str, size_usd: float,
                   opened_today: float = 0.0) -> str | None:
    """`ops.risk.check` against the simulated book instead of the live ledger.

    Same limits and the same order. `open_rows` are the positions held on the simulated
    day; the live version derives them from `decisions` minus exits/settles, which is the
    same set the sim tracks by entry day and settle day. `per_release_day_usd` is measured
    against the sim's own opens on that day, not the live ledger's.
    """
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.risk import LIMITS
    fam = REGISTRY[series].family if series in REGISTRY else "other"
    month = period[:7]
    ev = fam_ex = cl = gross = 0.0
    for p in open_rows:
        s = p["size_usd"]
        gross += s
        p_fam = REGISTRY[p["series"]].family if p["series"] in REGISTRY else "other"
        if p["series"] == series and p["period"] == period:
            ev += s
        if p_fam == fam:
            fam_ex += s
            if p["period"][:7] == month:
                cl += s
    if ev + size_usd > LIMITS["per_event_usd"]:
        return f"risk_per_event {ev + size_usd:.2f}"
    if fam_ex + size_usd > LIMITS["per_family_usd"]:
        return f"risk_per_family {fam_ex + size_usd:.2f}"
    if cl + size_usd > LIMITS["per_cluster_usd"]:
        return f"risk_cluster {cl + size_usd:.2f}"
    if gross + size_usd > LIMITS["gross_usd"]:
        return f"risk_gross {gross + size_usd:.2f}"
    if opened_today + size_usd > LIMITS["per_release_day_usd"]:
        return f"risk_release_day {opened_today + size_usd:.2f}"
    return None


def run(conn, days: int = 30, offset_hour: int = 16, bankroll: float = 100.0,
        max_lead_days: float | None = None, fair_mode: str = "model",
        end: datetime | None = None, db_gates: bool = True,
        select_params: bool = True, argmax_filter: bool = True,
        model_exits: bool = True, shadow_blocked: bool = False, log=None,
        param_override: dict[str, dict] | None = None,
        hash_tag: str = "", select_mode: str = "dsr",
        ml_stream: bool = True, census: bool = True,
        gate_params: bool = True) -> dict:
    """max_lead_days: entry allowed only when days-to-close <= this (the sweep
    knob — each lead sees DIFFERENT data and different predictions at its asof).

    end: last simulated day, default now. Bounding it is REQUIRED whenever the
    result is displayed next to the live ledger: unbounded, the sim runs to today
    and re-trades the same events the live ledger already holds, so the two
    segments double-count. `end` also bounds settlement — an event qualifies only
    if it settles by `end` — so every trade in the output both opens and settles
    inside the window and nothing straddles the cutover.

    fair_mode='pooled': decision fair = walk-forward log-pool of the model pmf
    with the DEVIGGED market pmf, weights ∝ 1/MSPE learned strictly inside the
    simulated timeline — an event's own outcome only enters the weights AFTER its
    close date passes in simulation (pending-score flush), so the pool at day D
    knows nothing later than D. Pooling with a sharper market kills fictional
    edges; surviving disagreements are where betting still makes sense.

    db_gates: rebuild skill / calibration / capture / conformal per simulated day from
    strictly-earlier closes and apply them in decide_all's order. On by default because
    the displayed backtest and the live ledger have to be the same strategy.

    select_params: ask `param_select` for each series' parameters as of the simulated
    day instead of using the registered defaults, matching production's daily
    re-selection. The gate inside it is unchanged — on a day it holds, the registered
    defaults are what comes back, and the run is then identical to --fixed-params.

    gate_params (#199): build the per-day gate history at those same selected params.
    Both switches above default True, and before this one existed that combination
    predicted at the selector's params while the skill / capture / conformal state was
    replayed at the registered defaults — a pairing that ran nowhere. Like `model_exits`
    this is a CORRECTNESS fix rather than a knob; False exists to reproduce the pre-#199
    numbers for A/B and nothing else. It has no effect when `select_params=False` and no
    `param_override` is set, because the per-day choice is then the defaults anyway.

    argmax_filter: keep `_place_argmax`'s defer-to-market rule (`fair <= cost`), which is
    what production does. False measures the counterfactual — the same favourite bought
    whether or not the model thinks the market is over-confident. This exists because the
    rule's own justification ("dual-window: fair<=cost won 27W-2L") was measured on the
    PRE-#109 harness, i.e. before the db-state gates were rebuilt PIT and before the
    bucket-devig fix, so it is not evidence about the strategy that runs today. It is a
    DIAGNOSTIC, not a knob to ship from: production keeps the filter until a change is
    pre-registered and forward-tested.

    model_exits (#142): run `ops/exits.py`'s rules inside the simulation instead of
    holding every position to settlement. This is a CORRECTNESS fix, not a knob — the
    live pipeline has always called `exits.run` (ops/refresh.py) while this harness
    jumped straight from entry to the 0/1 outcome, so every headline before it described
    a hold-to-settlement strategy production does not run. Same class of divergence as
    #109 and #128. `--no-model-exits` restores the old behaviour for A/B only.

    NOTE the granularity: live exits look at the book every refresh cycle, the simulation
    looks once a day at the candle close. So this reproduces the RULE faithfully and the
    TIMING coarsely, and it can only ever exit on or after the day live would have. That
    biases the modelled exit toward being late, never early, which is the safe direction
    for a harness whose job is to not flatter the strategy.

    NOTE also what #142 did NOT close: **re-entry after an exit**. Live, an exit removes
    the position from `ledger.open_positions`, so `decide()` sees `already_open=False` the
    next cycle and may buy the same (series, period) again if the edge comes back. Here
    `opened` is keyed by event and never cleared, so the simulation takes at most one
    trade per event either way. Leaving it is deliberate for now — clearing the key would
    turn `opened`/`opened_argmax` from a per-event dict into a list and rewrite how both
    streams, the hybrid join and `held_paths` are built, which is a larger change than the
    divergence justifies while exits are rare. The bias is that the simulation UNDERCOUNTS
    trades live would take, and `gate_stats['exit_*']` bounds how often it can bite: it
    cannot exceed the number of exits. Revisit if that count stops being small.

    shadow_blocked (#147): also emit a §25.3 feature row for events a GATE aborted —
    `skill_blocked` and `series_disabled` — marked `placed=False` / `blocked_by=<gate>`.
    Strictly a research flag, **default OFF**, and it changes no PnL in either state:
    blocked rows go only into `feature_rows`, never into `opened`/`opened_argmax`/
    `open_rows`/`opened_today`, and `trades` is built from `opened.values()` alone.

    Why it is needed: a feature row is written only for a bet that was PLACED, so a gated
    event leaves no trace. For `ser_roi` that is fatal rather than merely lossy — §25.4
    disables a series exactly when `ser_roi <= 0`, so the column is observed on 4 of 70
    rows and every one is positive. The feature is truncated at the very sign boundary its
    own premise is about (§25.17), and no coefficient fitted on it can test that premise in
    either direction. `skill_ratio` (9/70) is truncated by `skill_blocked` the same way.

    Two things it does NOT do, both load-bearing:

    * **It does not validate §25.4.** It makes the column READABLE. A verdict still has to
      come from a forward window per PREREGISTER §25.1 rule 1; re-running the 75-day window
      with this on is discovery, and discovery on a window already used for selection is
      exactly the thing that criterion exists to disqualify.
    * **It does not let blocked events feed anything the live path would not have seen.**
      In particular they are kept out of `pending_scores`, because those flush into
      `pool_runner` and set the `fair_mode='pooled'` log-pool weights — production
      `continue`s before the model runs on a gated series, so crediting its Brier scores
      here would move the pool weights, the pmf, the structs and finally the PnL. That is
      the one way this flag could have stopped being PnL-neutral, so it is guarded
      explicitly rather than left to the reader.

    ml_stream / census (2026-08-20): carry ALL of production's legs in THIS harness, so
    the choice of which to trade is made on one table instead of across incomparable
    ones. Before today `research/selector.py` reported `ml`'s ROI off its own ungated
    loop — no db_gates, no PIT gates, no exits, no risk veto, flat $1 stakes — and that
    number was being read next to `argmax`'s, which carries all of them. Two strategies
    measured on two harnesses cannot be ranked, and ranking them is the whole point.

      `ml_stream`  adds two lines. `ml` serves selector's learned p(win) INSIDE this
                   loop (see `_MLBook`), so it inherits db_gates, pit_gates, exits and
                   the risk veto and becomes comparable. `blend` then switches per event
                   between `hybrid` (the live rule) and `ml` on whichever has the better
                   TRAILING settled record — strictly point-in-time, so it is a rule you
                   could have run, not a hindsight pick of the winner.
      `census`     adds `arb` and `snipe`. Both are wired live and both have opened zero
                   positions, and a stream ABSENT from the backtest is indistinguishable
                   from one that found nothing. Neither can be sized here (candles carry
                   no depth, so 铁律 5 cannot bind), so both are counts, not PnL: how
                   many violations existed, how many would have cleared the fee gate, and
                   how many event-days had a book still open at the print.

    All four are COUNTERFACTUAL LINES. None charges the shared simulated wallet —
    `_open_rows` still returns the hybrid book alone — so `edge`/`argmax`/`hybrid` come
    back bit-identical to every stored run with these flags in either state. That is the
    property that makes it safe to leave them on by default, and it is asserted in
    tests/test_wf_streams.py rather than merely intended here.
    """
    from prediction_market_macro.model.ensemble import finite_pmf, log_pool
    now = datetime.now(timezone.utc)
    sim_end = end or now              # timeline horizon; `now` stays wall-clock provenance
    gates = dict(GATES)
    gates["min_leg_depth_usd"] = 0.0               # candles carry no depth
    if max_lead_days is not None:
        gates["max_days_to_close"] = float(max_lead_days)
    # walk-forward pool state (per series): cumulative Brier per source + pending
    # scores that unlock only when their event's close date passes in simulation
    pool_runner: dict[str, dict[str, list[float]]] = {}
    pending_scores: list[tuple[datetime, str, float, float]] = []  # (close, series, bm, bk)
    # #147: (series, period) of gate-blocked events already shadow-logged, so a blocked
    # event contributes one row per stream rather than one per simulated day
    blocked_seen: set[tuple[str, str]] = set()
    # (calendar, period) -> scheduled release, for `decide()`'s freeze_window gate. Cached
    # because the event loop re-visits the same event on every simulated day until close.
    release_cache: dict[tuple[str, str], datetime | None] = {}

    def _pool_weights(series: str) -> dict[str, float] | None:
        r = pool_runner.get(series)
        if not r or len(r.get("model", [])) < 3:
            return None
        mm = sum(r["model"]) / len(r["model"])
        mk = sum(r["market"]) / len(r["market"])
        wm_raw, wk_raw = 1.0 / max(mm, 1e-6), 1.0 / max(mk, 1e-6)
        wm = max(0.10, min(0.90, wm_raw / (wm_raw + wk_raw)))
        return {"model": wm, "market": 1.0 - wm}
    opened: dict[tuple[str, str], dict] = {}       # (series, key) -> edge trade
    opened_argmax: dict[tuple[str, str], dict] = {}  # favourite (argmax) stream
    held_paths: dict[str, list] = {}                 # §25.5 diagnostic, see `_mtm_path`
    # §25.3 training set for the per-bet confidence model. Kept OUT of the trade rows
    # for the same reason `held_paths` is: `freeze_track` copies trade rows verbatim into
    # the public track payload, and the model's inputs are research state, not a
    # customer-facing record. Every field here is knowable AT THE MOMENT OF THE BET —
    # that is the whole contract of this list, and `research/confidence.py` relies on it.
    feature_rows: list[dict] = []
    if param_override and not hash_tag:
        raise ValueError("param_override requires a hash_tag — an untagged override "
                         "run would overwrite the canonical experiments row")
    if select_mode not in ("dsr", "argmin"):
        raise ValueError(f"unknown select_mode {select_mode!r}")
    if select_mode == "argmin" and not select_params:
        raise ValueError("select_mode='argmin' is a selector mode — it requires "
                         "select_params=True")
    book = _GateBook(conn, db_gates=db_gates, select_params=select_params, log=log,
                     param_override=param_override, select_mode=select_mode,
                     argmin_start=sim_end - timedelta(days=days),
                     argmin_end=sim_end, gate_params=gate_params)

    def _open_rows(on_day: datetime) -> list[dict]:
        """The simulated book on `on_day`: entered on or before it, not yet closed out.
        Both streams count, because `ops.risk._open_exposure` counts both.

        But at most ONE leg per event, which is the HYBRID book — the rule live actually
        runs. `decide_all` calls `_place_argmax` only when the edge decision was a pass,
        and `_has_any_open` bars it regardless, so live can never hold both legs of one
        event; the two standalone streams below are counterfactual lines sharing one
        simulated wallet. Charging both to that wallet double-counted the exposure of
        every event where the edge stream fired. Inert on this window (`risk_vetoed` is 0,
        so no trade was ever displaced by the overcharge) and left inert deliberately: the
        `opened_argmax` entries themselves are untouched, so the standalone argmax stream
        still reports every leg it took.

        #142: a position that exited early stops consuming risk limits from its exit day,
        which is what `ledger.open_positions` does live — it drops a (series, period) the
        moment an 'exit' row lands. Keeping it on the book to settlement would have the
        simulation refuse trades on exposure it no longer has."""
        rows = []
        hybrid_book = list(opened.values()) + [t for k, t in opened_argmax.items()
                                               if k not in opened]
        for t in hybrid_book:
            if t["day"] <= on_day.date().isoformat() < (t.get("exit_day")
                                                        or t["settle"]):
                rows.append({"series": t["series"], "period": t["period"],
                             "size_usd": t["staked"]})
        return rows

    # ── counterfactual lines (2026-08-20) ────────────────────────────────────────
    # None of the state below is read by `_open_rows`, so none of it charges the shared
    # simulated wallet or can displace a hybrid trade through `_sim_risk_veto`.
    opened_ml: dict[tuple[str, str], dict] = {}      # learned selector, gated HERE
    opened_blend: dict[tuple[str, str], dict] = {}   # PIT switch between hybrid and ml
    mlbook = _MLBook(conn) if ml_stream else None
    # `blend`'s trailing comparison needs each line's own settled record in the order it
    # settled. `hybrid` is the live rule, so that is what ml is measured against — not
    # `argmax` as in selector.py, where no hybrid existed to compare to.
    def _rows_of(trades, on_day) -> list[dict]:
        """`_open_rows`' exposure shape for an ARBITRARY book — so the ml line's risk
        veto is measured against ml's own positions, not the hybrid book's. Vetoing a
        shadow stream on somebody else's exposure would make its trade count a function
        of what the live rule happened to be holding, which is the one thing a
        counterfactual is supposed to hold constant."""
        return [{"series": t["series"], "period": t["period"], "size_usd": t["staked"]}
                for t in trades
                if t["day"] <= on_day.date().isoformat()
                < (t.get("exit_day") or t["settle"])]

    def _trail(trades, before_iso: str) -> list[float]:
        """The last `selector.TRAIL` realised PnLs a line had SETTLED before a day.

        Strictly point-in-time, and the day used is the day the cash actually landed —
        `exit_day` when #142's exit rules closed the position early, `settle` otherwise.
        Keying it on `settle` alone would let an exited trade sit invisible in the record
        for days after its money was already back, which is information the switch has
        and the rule would be pretending it does not."""
        from prediction_market_macro.research.selector import TRAIL
        done = sorted((t for t in trades
                       if (t.get("exit_day") or t["settle"]) < before_iso),
                      key=lambda t: (t.get("exit_day") or t["settle"]))
        return [t["realized"] for t in done][-TRAIL:]

    # `arb`/`snipe` census. Counts, not dollars — see `_arb_scan`.
    cen = {"arb_event_days": 0, "arb_violations": 0, "arb_would_clear": 0,
           "arb_best_net": None, "arb_unpriceable": 0,
           # snipe is counted twice on purpose. `in_scope` is the six series
           # `strategy/snipe.SNIPE_SERIES` actually watches — that is the number that
           # explains the live stream's zero. `any_series` is every series this loop
           # sees, and the two differing is itself the finding: a post-print window
           # that exists on a book nobody is pointed at is a wiring gap, not a
           # structural wall, and one aggregate number would hide which one this is.
           "snipe_event_days": 0, "snipe_book_open_at_print": 0,
           "snipe_event_days_in_scope": 0, "snipe_open_at_print_in_scope": 0,
           "snipe_no_release_ts": 0,
           # how many event-days the ml line never got to see because the loop had
           # already retired the event (both live legs filled). Bounds the handicap
           # instead of leaving it as an unquantified asterisk — see the skip at the
           # top of the event loop.
           "ml_event_days_skipped": 0}
    daily: list[dict] = []
    for d in range(days, 0, -1):
        day = (sim_end - timedelta(days=d)).replace(hour=offset_hour, minute=0,
                                                    second=0, microsecond=0)
        day_iso = day.date().isoformat()
        open_rows = _open_rows(day)
        opened_today = 0.0
        # flush pending pool scores whose events have CLOSED by simulated `day`
        # (an event's own outcome must never influence its own pool weights)
        if fair_mode == "pooled":
            still = []
            for cts, ser, bm, bk in pending_scores:
                if cts <= day:
                    r = pool_runner.setdefault(ser, {"model": [], "market": []})
                    r["model"].append(bm)
                    r["market"].append(bk)
                else:
                    still.append((cts, ser, bm, bk))
            pending_scores[:] = still
        day_trades = 0
        opened_today_ml = 0.0            # ml's own daily stake, for its own risk veto
        for ev in _open_settled_events(conn, day, sim_end):
            key = kalshi_period_to_key(ev["tok"])
            if not key or ((ev["series"], key) in opened
                           and (ev["series"], key) in opened_argmax):
                # both LIVE streams already entered. The counterfactual `ml` line is
                # deliberately NOT added to this condition: running the event on further
                # days would increment `book.stats`, re-run `param_select`, and append to
                # `pending_scores` — which sets the `fair_mode='pooled'` weights and so
                # moves the hybrid pmf and its PnL. A shadow stream that changes the
                # measured strategy is worse than a shadow stream with a bounded blind
                # spot, so the blind spot is counted instead of closed.
                if ml_stream and key and (ev["series"], key) not in opened_ml:
                    cen["ml_event_days_skipped"] += 1
                continue
            spec = REGISTRY[ev["series"]]
            legs_rows = conn.execute(
                "SELECT c.ticker, c.floor_strike, c.cap_strike, c.strike_type,"
                " c.close_time, s.result FROM contracts c"
                " JOIN settlements s ON s.ticker=c.ticker"
                " WHERE c.series=? AND s.period=? AND s.result IN ('yes','no')",
                (ev["series"], ev["tok"])).fetchall()
            meta, results = [], {}
            for l in legs_rows:
                b, a = _candle_quote(conn, l["ticker"], day)
                if b is None and a is None:
                    continue
                meta.append({"ticker": l["ticker"], "strike": l["floor_strike"],
                             "cap_strike": l["cap_strike"],
                             "strike_type": l["strike_type"],
                             "close_time": l["close_time"], "yes_bid": b,
                             "yes_ask": a, "bid_depth": 1e9, "ask_depth": 1e9})
                results[l["ticker"]] = l["result"]
            if not meta:
                continue
            # §25.3 feature: the book's median bid/ask width on this event, on THIS day.
            # Computed here rather than inside the db_gates branch below so the feature
            # exists on ungated diagnostic runs too — a training set whose columns appear
            # and vanish with an unrelated flag is not a training set.
            from prediction_market_macro.model.ensemble import median_spread as _msp
            ev_spread = _msp(meta)
            # §skill BLOCK comes first and aborts the whole event, argmax leg included —
            # `decide_all` `continue`s here, so a hybrid that keeps buying favourites on
            # a blocked series would not be the live rule.
            gs = book.gates_for(ev["series"], day)
            # #147: `blocked_by` replaces what used to be two bare `continue`s. When
            # `shadow_blocked` is off (the default) the `continue` below fires exactly where
            # they did and this is a pure rename. When it is on, the event runs the whole
            # way through so a feature row can be written for it — but it is never booked,
            # and never touches `pending_scores`.
            # The ENTRY-side circuit breaker (`decide_all` 铁律 10, which sits ahead of
            # every gate below and of the already-open check) is likewise NOT in this
            # chain, and unlike rule 2 in `_first_exit` it HAS fired live — so it is a
            # disclosed structural gap, not an unfired one. Measured over the whole
            # ledger: exactly 1 decision ever cites it (id 2340, KXWTIW 2026-07-31 at
            # 20:22:25Z, `health_red:replay_mismatch`), against 24 breaker alerts.
            #
            # It cannot be ported, for a reason in the schema rather than the effort:
            # `breaker_tripped` reads UNACKED alerts, and `alerts` stores `acked` as a
            # bare flag with no ack timestamp. At simulated day D there is no way to know
            # whether an alert written before D had been acked before or after D — the
            # ack is an operator action with no PIT record. Simulating it would mean
            # modelling operator behaviour, which is strictly more modelling risk than
            # the gap it closes.
            #
            # Measured effect on this harness: zero. The one live citation is at 20:22Z
            # and `run` takes one decision per simulated day at `offset_hour`=16:00Z; at
            # that instant on 07-31 the breaker was not tripped for KXWTIW live either
            # (the 16:01 and 16:13 cycles both returned already_open_no_averaging_down).
            blocked_by = None
            if gs is not None and gs.blocked is not None:
                book.stats["skill_blocked"] += 1
                blocked_by = "skill_blocked"
            # §25.4 per-series switch — in `decide_all`'s order (after the skill block)
            # and with the same consequence: the whole event is skipped, argmax included.
            elif gs is not None and gs.disabled is not None:
                book.stats["series_disabled"] += 1
                blocked_by = "series_disabled"
            # #155: while `series_enable.SHADOW` is on, `gs.disabled` is always None and
            # the event trades. Count what the gate WOULD have refused, under the same
            # precedence the veto would have had — only where the skill block did not
            # already abort the event — so the shadow count and the live count are the
            # same quantity read in two states, and exactly one of them is ever nonzero.
            if gs is not None and blocked_by is None and gs.would_disable is not None:
                book.stats["series_disabled_shadow"] += 1
            if blocked_by is not None:
                # one shadow row per (event, stream), not one per simulated DAY. A blocked
                # event never enters `opened`, so without this it would be re-examined
                # every day until close and emit ~7 near-duplicate rows — which would both
                # swamp the 70 placed rows and violate the independence the fold assumes.
                if not shadow_blocked or (ev["series"], key) in blocked_seen:
                    continue
                blocked_seen.add((ev["series"], key))
            disp = SERIES_DISPATCH[ev["series"]]
            fn = getattr(importlib.import_module(disp[0]), disp[1])
            try:
                pred = fn(conn, day, key, series=ev["series"],
                          params=book.params_for(ev["series"], day))
            except Exception:                      # noqa: BLE001
                continue
            # §27.4 information gate — parity with `ops/decide_all._aaa_information_gate`,
            # which lives here rather than above only because it needs the pred's `mode`.
            #
            # That function's docstring claimed the backtest needed no equivalent, "since
            # every AAA_DAILY row carries knowledge_time >= 2026-07-31, so any replay
            # before then takes the proxy branch by construction". The premise is true and
            # the conclusion is backwards: the proxy branch IS the blocking condition, so a
            # replay before that date takes exactly the branch live refuses to trade. All 6
            # KXAAAGASW trades in the displayed d75 window are dated 05-24..06-23 — every
            # one of them booked in a state the live lane would have passed on.
            #
            # They lost 6.9% against the book's 29.7%, so removing them makes the published
            # number WORSE (d75 -29.66% -> -34.40%). That is the direction that matters: a
            # backtest may not be credited with trades its live twin refuses.
            if (blocked_by is None and ev["series"] == "KXAAAGASW"
                    and (pred.inputs or {}).get("mode") != "aaa_daily_anchor"):
                book.stats["aaa_proxy_only"] += 1
                blocked_by = "aaa_proxy_only"
                if not shadow_blocked or (ev["series"], key) in blocked_seen:
                    continue
                blocked_seen.add((ev["series"], key))
            import math as _math
            entropy_norm = None
            if isinstance(pred.dist, Categorical):
                probs = dict(pred.dist.probs)
                if fair_mode == "pooled":
                    asks = {l["ticker"].rsplit("-", 1)[-1]: l["yes_ask"]
                            for l in meta if l.get("yes_ask")}
                    tot = sum(asks.values())
                    w = _pool_weights(ev["series"])
                    if tot > 0 and w:
                        mkp = {k: v / tot for k, v in asks.items()}
                        lp = {k: w["model"] * _math.log(max(probs.get(k, 0), 1e-6))
                              + w["market"] * _math.log(max(mkp.get(k, 0), 1e-6))
                              for k in set(probs) | set(mkp)}
                        mx = max(lp.values())
                        ex = {k: _math.exp(v - mx) for k, v in lp.items()}
                        z = sum(ex.values())
                        probs = {k: v / z for k, v in ex.items()}
                structs = _structs_categorical(meta, probs)
                pv = [v for v in probs.values() if v > 0]
                model_mean, market_fairs = None, None   # both are ladder-only in prod
                wide_book = False                       # so is the #130 width check
            else:
                pmf = grid_pmf(pred.dist, spec.round_rule)
                mk_pmf = None
                if fair_mode == "pooled":
                    impl = _implied(meta)
                    mk_pmf = finite_pmf({float(k): v
                                         for k, v in (impl.get("pmf") or {}).items()})
                    w = _pool_weights(ev["series"])
                    if mk_pmf and w:
                        pmf = log_pool({"model": pmf, "market": mk_pmf}, w)
                structs = enumerate_structs(meta, pmf, strict=strict_gt_at(spec.ticker, day))
                pv = [v for v in pmf.values() if v > 0]
                model_mean = sum(k * v for k, v in pmf.items())
                # decide()'s sanity gate compares the model fair against the DEVIGGED
                # market prob in production and against the raw ask here whenever this
                # is left None — a silently looser gate. Devig the same book prod does.
                market_fairs, wide_book = None, False
                if db_gates:
                    _im = _implied(meta)
                    from prediction_market_macro.model.ensemble import (WIDE_SPREAD,
                                                                        median_spread)
                    sp = median_spread(meta)
                    # a wide book no longer DROPS the reference (#130) — it is recorded
                    # and applied below, after calibration, where prod applies it
                    wide_book = sp is not None and sp > WIDE_SPREAD
                    if _im.get("pmf"):
                        market_fairs = {ms.desc: ms.fair for ms in enumerate_structs(
                            meta, {float(k): v for k, v in _im["pmf"].items()},
                            strict=strict_gt_at(spec.ticker, day))}
                # queue this event's model/market scores for post-close flush
                if fair_mode == "pooled" and mk_pmf:
                    from prediction_market_macro.model.common import leg_fair
                    bm_s, bk_s, n_l = 0.0, 0.0, 0
                    raw_pmf = grid_pmf(pred.dist, spec.round_rule)
                    for m in meta:
                        res = results.get(m["ticker"])
                        if res is None or m["strike"] is None:
                            continue
                        try:
                            fm = leg_fair(raw_pmf, effective_strike_type(spec.ticker, m["strike_type"], asof=day),
                                          m["strike"], m["cap_strike"])
                            fk = leg_fair(mk_pmf, effective_strike_type(spec.ticker, m["strike_type"], asof=day),
                                          m["strike"], m["cap_strike"])
                        except Exception:                  # noqa: BLE001
                            continue
                        out01 = 1.0 if res == "yes" else 0.0
                        bm_s += (fm - out01) ** 2
                        bk_s += (fk - out01) ** 2
                        n_l += 1
                    # #147: a gate-blocked event must NOT score the pool. Production
                    # `continue`s before the model runs on a blocked series, so its Brier
                    # scores were never available to the live pool weights either — and
                    # `pending_scores` flushes into `pool_runner`, which sets the pooled
                    # pmf and therefore the structs and the PnL. This guard is the reason
                    # `shadow_blocked` is PnL-neutral rather than merely intended to be.
                    if blocked_by is None and n_l and not any(
                            s2 == ev["series"] and c2 == ev["close_ts"]
                            for c2, s2, *_ in pending_scores):
                        pending_scores.append((ev["close_ts"], ev["series"],
                                               bm_s / n_l, bk_s / n_l))
            if len(pv) > 1:
                entropy_norm = -sum(p * _math.log(p) for p in pv) / _math.log(len(pv))

            # ── the remaining db-state gates, in decide_all's order ──────────────
            gates_eff = dict(gates)
            if gs is not None:
                before_cal = [st.fair for st in structs]
                structs = gs.calibrate_structs(structs)
                if any(a != b.fair for a, b in zip(before_cal, structs)):
                    book.stats["calibrated_events"] += 1
                structs, cap_drops = gs.filter_capture(
                    structs, model_mean, spec.round_rule,
                    {m["ticker"]: m["strike"] for m in meta})
                book.stats["capture_dropped"] += len(cap_drops)
                if not structs:
                    continue
                # §19-8 structural-break flags, PIT: a flag raised after `day`, or one
                # that had already expired by it, was not visible to the live path then.
                fl = conn.execute(
                    "SELECT MAX(severity) sev FROM event_flags WHERE family=?"
                    " AND ts<=? AND expires_ts>?",
                    (spec.family, day.isoformat(), day.isoformat())).fetchone()
                if fl and fl["sev"]:
                    gates_eff["max_size_usd"] *= 0.5
                    gates_eff["max_entropy_norm"] = (gates_eff["max_entropy_norm"]
                                                     - 0.02 * fl["sev"])
                if gs.conformal_factor != 1.0:
                    book.stats["conformal_throttled"] += 1
                if gs.defensive is not None:
                    book.stats["skill_defensive"] += 1
                gates_eff = gs.apply_gates(gates_eff)

            # #142 — what a held position's exit check needs on a LATER day: this
            # event's strike metadata (fixed), and the model re-run at that day.
            leg_meta = {m["ticker"]: {"strike": m["strike"],
                                      "cap_strike": m["cap_strike"],
                                      "strike_type": m["strike_type"]} for m in meta}
            _pmf_cache: dict[str, dict | None] = {}

            def _pmf_for(d: datetime, _fn=fn, _spec=spec, _ser=ev["series"], _k=key):
                """The model as of a later simulated day, in `exits.run`'s shape.

                RAW model output, never the `fair_mode='pooled'` blend that the entry
                path may have used: `ops/predict_all` writes the model's own pmf to
                `preds`, and that is the row `exits.run` reads. Pooling the exit metric
                with the market would compare the market against itself.

                Parameters come from the same PIT daily selector, re-asked for `d` —
                pinning entry day's parameters would freeze the model mid-flight while
                production kept re-selecting.
                """
                ck = d.date().isoformat()
                if ck in _pmf_cache:
                    return _pmf_cache[ck]
                out_m: dict | None = None
                try:
                    p = _fn(conn, d, _k, series=_ser, params=book.params_for(_ser, d))
                    out_m = ({"probs": dict(p.dist.probs)}
                             if isinstance(p.dist, Categorical)
                             else {"pmf": grid_pmf(p.dist, _spec.round_rule)})
                except Exception:                                # noqa: BLE001
                    out_m = None
                _pmf_cache[ck] = out_m
                return out_m

            def _settle_y(st):
                """1 if the structure paid, 0 if it did not, None if unresolvable.

                PR-7's label. Read off `results` rather than from the sign of `realized`
                for two reasons: `realized` is OVERWRITTEN by the exit price when #142's
                rules fire, so on an exited trade its sign says nothing about settlement;
                and even on a held trade `realized > 0` is only sign-exact while
                `1 - cost` exceeds the fees, which fails on a bucket bought near $1.

                A bucket pays iff BOTH legs win — YES(>lo) and NO(>hi) is one binary.
                """
                for leg in st.legs:
                    res = results.get(leg.ticker)
                    if res is None:
                        return None
                    if res != leg.side:
                        return 0
                return 1

            def _settle_struct(st, count):
                # the body of this used to live here; it is now `edge.settle_struct` so
                # that the PR-2 shadow scorer grades a trade with the SAME arithmetic
                # instead of a lookalike copy (#141). Delegation, not a reimplementation:
                # same loop, same order, bit-identical.
                from prediction_market_macro.strategy.edge import settle_struct
                return settle_struct(st.legs, count, results)

            def _trade_row(st, count, realized, stream, shadow: bool = False):
                # `shadow=True` (the ml/blend counterfactual lines, 2026-08-20) runs the
                # IDENTICAL arithmetic — same fees, same `_mtm_path`, same `_first_exit`,
                # same `staked` convention — and writes NOTHING to the shared artefacts:
                # no `feature_rows` row, no `held_paths` entry, no `book.stats` exit
                # counter. That is what keeps edge/argmax/hybrid bit-identical with the
                # new streams on. The three writes are the only side effects in here, so
                # guarding exactly them is the whole of the isolation; in particular
                # `held_paths` is keyed `series/period/desc` with NO stream component, so
                # an ml pick that lands on the same struct as the edge pick would
                # otherwise silently OVERWRITE the edge stream's published path.
                #
                # It is one function rather than two because a copy would drift: the
                # comparison "is the learned line better?" is only meaningful while both
                # lines are priced by the same code, and a second implementation of the
                # exit rules is exactly how that stops being true.
                # 2026-08-11 display convention (user instruction): `staked` = net
                # premium PLUS entry taker fees — the full cash a losing position
                # forfeits — so ROI bottoms out at exactly -100% instead of running
                # past it by the fee. `realized` already nets the same fees, so
                # numerator and denominator now describe the same cash.
                entry_fees = round(sum(taker_fee(l.price, count) for l in st.legs), 4)
                lead_days = (ev["close_ts"] - day).total_seconds() / 86400.0
                # captured BEFORE #142's exit can overwrite `realized` — see `_settle_y`
                settle_y = _settle_y(st)
                # PR-7's m_0: the MARKET's implied probability at entry, on the same
                # scale as `_mtm_path`'s `m_mid`. Not `st.cost` — that is the ask side,
                # so differencing it against later mids would read half a spread of
                # drawdown on a position that never moved.
                #
                # None when the entry book was ONE-SIDED. `enumerate_structs` needs only
                # `yes_ask` to build a yes leg and only `yes_bid` to build a no leg, so a
                # struct can legitimately exist with no mid at all — and PR-7 drops those
                # rows rather than substituting the touch, which would manufacture the
                # drawdown the test is looking for.
                entry_m = _struct_mid(st, {m["ticker"]: m for m in meta})
                # ── §25.5 held path + #142 exit rules ──────────────────────────────
                # The path is computed here, at entry, and that is not a PIT violation:
                # every row in it reads only candles and a re-prediction AS OF its own
                # day, and an exit found on day X only ever removes capital from days
                # AFTER X (`_open_rows` keys on `exit_day`). Computing it eagerly is a
                # scheduling detail; the information flow is still strictly forward.
                path = _mtm_path(conn, st, count, day, ev["close_ts"], offset_hour,
                                 pmf_for=_pmf_for if model_exits else None,
                                 leg_meta=leg_meta)
                ex = _first_exit(st, path, gates_eff.get("min_leg_price", 0.10)) \
                    if model_exits else None
                if ex is not None:
                    # #142: the position was CLOSED, so its realized PnL is the exit
                    # proceeds — not the settlement outcome the old harness always used.
                    # `mtm` already nets entry fee, exit fee and exits.SLIP, which is the
                    # same arithmetic `ops/exits._write_exit` books live.
                    realized = ex["mtm"]
                    if not shadow:
                        book.stats[f"exit_{ex['rule']}"] += 1
                    # everything after the exit is somebody else's PnL — truncate so the
                    # §25.5 oracle cannot claim a peak we were no longer holding
                    path = [p for p in path if p["day"] <= ex["day"]]
                # ── §25.3 confidence-model row ────────────────────────────────────
                # Everything below is knowable at the moment of the bet. Two of them are
                # worth naming because they are the ones that could silently stop being
                # PIT: `skill_ratio`/`ser_roi` come from `gs`, which `pit_gates.asof`
                # built from events that closed STRICTLY before `day`; and `realized`
                # is the label, never a feature. If a future edit reads `gs` from the
                # stored artefacts instead, this whole table becomes leakage.
                _es = (gs.enable_state if gs is not None else None) or {}
                # §25.3's table is the record of bets the LIVE rule placed. A shadow
                # line's picks in it would train `research/confidence.py` on a strategy
                # nobody runs, so they go to a throwaway list instead of `feature_rows`.
                _fr_sink = [] if shadow else feature_rows
                _fr_sink.append({
                    "series": ev["series"], "period": key, "day": day.date().isoformat(),
                    "settle": ev["close_ts"].date().isoformat(), "desc": st.desc,
                    "stream": stream,
                    # #147. `placed=False` rows are COUNTERFACTUAL: a gate stopped the bet,
                    # so `realized` is what it would have returned, not money that moved.
                    # They are absent from `trades` and from every PnL aggregate. Anything
                    # fitting on this table must decide explicitly whether it wants them —
                    # `staked` is the would-be stake, NOT zero, because the label has to
                    # stay on one scale with the placed rows to be poolable at all.
                    "placed": blocked_by is None, "blocked_by": blocked_by,
                    # — the bet itself —
                    "fair": round(st.fair, 6), "cost": round(st.cost, 6),
                    "net_edge": round(st.net_edge(count), 6),
                    # #129 §4's strongest single variable. Signed, because "model above
                    # market" and "model below market" priced differently (-6.66% vs
                    # -16.54%); `abs_div` carries the magnitude the plan wants monotone.
                    "divergence": round(st.fair - st.cost, 6),
                    "abs_div": round(abs(st.fair - st.cost), 6),
                    "kind": st.kind, "n_legs": len(st.legs),
                    # — the book —
                    "spread": None if ev_spread is None else round(ev_spread, 6),
                    "lead_days": round(lead_days, 3),
                    "entropy_norm": (None if entropy_norm is None
                                     else round(entropy_norm, 6)),
                    # — the series' state on that day, straight off the PIT gates —
                    "skill_ratio": (None if gs is None or gs.skill_ratio is None
                                    else round(gs.skill_ratio, 6)),
                    "conformal_factor": (1.0 if gs is None
                                         else float(gs.conformal_factor)),
                    "defensive": bool(gs is not None and gs.defensive is not None),
                    # trailing-12 realised ROI of the series, already folded by §25.4 —
                    # free, and PIT by construction (same `earlier` prefix)
                    "ser_roi": _es.get("roi"), "ser_n": _es.get("n", 0),
                    # — the label —
                    "staked": round(st.fill_cost(count) * count + entry_fees, 4),
                    "realized": round(realized, 4), "won": realized > 0,
                    "exit_day": None if ex is None else ex["day"],
                    "exit_rule": None if ex is None else ex["rule"]})
                peak = max(path, key=lambda p: p["mtm"]) if path else None
                peak_mid = max((p["mtm_mid"] for p in path), default=None)
                # The path itself is kept OUT of the trade row. `freeze_track` copies
                # these rows verbatim into the frozen public track payload, so anything
                # added here is published; a per-day diagnostic array does not belong in
                # a customer-facing record and would multiply its size. It is collected
                # separately below and exposed as `held_paths`.
                if path and blocked_by is None and not shadow:  # #147: no held path for a bet nobody held
                    # PR-7 pairs each daily mid with the outcome that mid was forecasting,
                    # so the label and the entry mid ride on the rows rather than in a
                    # parallel structure `freeze_track`/`confidence` would have to learn.
                    for _p in path:
                        _p["settle_y"] = settle_y
                        _p["entry_m"] = (None if entry_m is None
                                         else round(entry_m, 6))
                    held_paths[f"{ev['series']}/{key}/{st.desc}"] = path
                return {"series": ev["series"], "period": key,
                        "day": day.date().isoformat(), "desc": st.desc,
                        "fair": round(st.fair, 4), "cost": round(st.cost, 4),
                        "count": count,
                        # capital at risk, on production's definition. `decide` books
                        # size_usd = count * fill_cost(count), and fill_cost returns the
                        # NET debit (sum(px) - 1) on a bucket, because one of the two legs
                        # pays in every branch. Summing leg prices instead charges a bucket
                        # its gross notional — ~$1 per contract too much — which does not
                        # change pnl but inflates the ROI DENOMINATOR, so the backtest reads
                        # better than it is. Measured on the displayed 75-day window: 6
                        # bucket trades booked $22.53 against $4.53 of real risk, and the
                        # headline hybrid ROI read -9.17% instead of -13.21%.
                        "staked": round(st.fill_cost(count) * count + entry_fees, 4),
                        "realized": round(realized, 4), "won": realized > 0,
                        "lead_days": round(lead_days, 1),
                        "settle": ev["close_ts"].date().isoformat(),
                        # #142. `settle` stays the event's settlement date because that
                        # is what the trade was ON; `exit_day` is when the capital came
                        # back, and it is what the equity curve and `_open_rows` key off.
                        "exit_day": None if ex is None else ex["day"],
                        "exit_rule": None if ex is None else ex["rule"],
                        "exit_hold_edge": None if ex is None else round(ex["hold_edge"], 4),
                        # §25.5. `mtm_days` is how many daily observations existed at
                        # all — a trade opened one day before close has none, and an
                        # empty path must read as "not observed", never as "never green".
                        "mtm_days": len(path),
                        "mtm_peak": None if peak is None else peak["mtm"],
                        "mtm_peak_day": None if peak is None else peak["day"],
                        "mtm_peak_mid": None if peak_mid is None else round(peak_mid, 4)}

            # ── #130: a wide book tightens the sanity gate, it does not switch it off.
            # Placed HERE, not where market_fairs is built, because `decide_all` applies
            # it after calibration and capture — and calibration moves `st.fair`, which
            # is what the "further from our fair" choice is measured against. Building it
            # earlier would compare against pre-calibration fairs and silently diverge
            # from production on exactly the events this gate exists for.
            if wide_book:
                _mf = market_fairs or {}
                market_fairs = {st.desc: max(_mf.get(st.desc, st.cost), st.cost,
                                             key=lambda p, f=st.fair: abs(f - p))
                                for st in structs}
            # ── stream 1: EDGE (value) line — the existing gated decision ──
            # `release_ts` was hardcoded None, which switches `decide()`'s freeze_window
            # gate OFF for the whole simulation while `decide_all` and `eval.decision_replay`
            # both pass the real value. Read from the SAME place live reads it (the
            # `releases` table by (calendar, period)) — parity means running live's rule
            # off live's source, not a better one. A scheduled release time is a forward
            # calendar entry, so knowing it on the simulated day is not look-ahead.
            #
            # Inert on the current book and measured as such: the sim clock is
            # `offset_hour`=16:00Z, the freeze window is the 10 minutes before a release,
            # and 0 of 548 release rows fall in 15:50-16:00 (they cluster at 18:30/14:30/
            # 12:30). It silently activates the day a release time moves, which is the
            # whole reason not to leave a gate wired to None.
            rk = (spec.calendar, key)
            if rk not in release_cache:
                _rel = conn.execute(
                    "SELECT scheduled_ts FROM releases WHERE cal=? AND period=?", rk
                ).fetchone()
                release_cache[rk] = (datetime.fromisoformat(_rel["scheduled_ts"])
                                     if _rel else None)
            dec = decide(structs, now=day, close_time=ev["close_ts"],
                         release_ts=release_cache[rk], market_implied=market_fairs,
                         already_open=(ev["series"], key) in opened,
                         bankroll=bankroll, gates=gates_eff, entropy_norm=entropy_norm)
            take = dec.action == "open" and dec.struct is not None
            if take and db_gates and _sim_risk_veto(open_rows, ev["series"], key,
                                                    dec.size_usd, opened_today):
                # a risk veto turns the decision into a pass in prod, which leaves the
                # argmax leg below free to fire — same here
                book.stats["risk_vetoed"] += 1
                take = False
            # what the LIVE rule bought on this event-day, if anything: the edge bet
            # where it fired, the favourite where edge passed. `blend` switches against
            # this, not against `argmax` alone the way selector.py does — selector had no
            # hybrid line to compare to, and hybrid is what production actually runs.
            hyb_row = None
            if take:
                realized = _settle_struct(dec.struct, dec.count)
                if realized is not None:
                    row = _trade_row(dec.struct, dec.count, realized, "edge")
                    # #147: `_trade_row` has already written the feature row. Booking is
                    # what a blocked event must not do — no `opened`, so no `trades`, no
                    # equity curve, no capital consumed by `_sim_risk_veto` on later days.
                    if blocked_by is None:
                        opened[(ev["series"], key)] = row
                        open_rows.append({"series": ev["series"], "period": key,
                                          "size_usd": row["staked"]})
                        opened_today += row["staked"]
                        day_trades += 1
                        hyb_row = row
            # ── stream 2: ARGMAX (favourite) line — WC hybrid's other leg: buy
            # the MODEL's most likely structure, flat $1, no edge requirement.
            # Price window [0.10, 0.90] keeps payoff room net of fees. ──
            if (ev["series"], key) not in opened_argmax:
                _dtc = (ev["close_ts"] - day).total_seconds() / 86400.0
                # decide_all gates this leg on 0.03 <= dtc <= max_days_to_close; the
                # lower bound (~43 min) is what keeps it out of the last minutes before
                # close, where the edge stream is already barred by min_minutes_to_close
                inside = 0.03 <= _dtc <= gates_eff.get("max_days_to_close", 7.0)
                # defer-to-the-stronger-forecaster: bet the favourite only when
                # the MARKET's confidence >= the model's (fair <= cost). A weaker
                # model claiming the favourite is underpriced is adverse
                # selection (dual-window: fair>cost lost -15%/-26%; fair<=cost
                # won 27W-2L across 60d+30d)
                cands = [st for st in structs
                         if 0.10 <= st.cost <= 0.90 and st.fair > 0.5]
                if inside and cands:
                    st_a = max(cands, key=lambda x: x.fair)
                    # select-THEN-filter: the favourite is the max-fair pick; it
                    # qualifies only when the market's confidence >= the model's
                    if argmax_filter and st_a.fair > st_a.cost:
                        st_a = None
                    # #148 — `_place_argmax`'s churn guard: refuse a favourite that rule 1
                    # would liquidate on the same cycle. `_hold_edge` is this module's
                    # designated port of that rule, so the guard is the port applied at
                    # entry rather than a second reading of it.
                    #
                    # This harness could not have SEEN the churn it prevents: `_mtm_path`
                    # starts at `entry_day + 1`, so a same-cycle round trip is outside the
                    # grid by construction, and all three live ones happened inside 219ms.
                    # The backtest therefore booked those trades as held while production
                    # was paying the round trip — one more #109/#151-shaped divergence, and
                    # the guard closes it from the entry side in BOTH lanes at once, which
                    # is why it needs no intra-day exit model to fix.
                    #
                    # Tied to `model_exits` because it is derived from the exit policy:
                    # `--no-model-exits` asks what these positions do if held, and a guard
                    # that refuses to open them would answer a different question.
                    if st_a is not None and model_exits:
                        from prediction_market_macro.ops.exits import EXIT_EDGE as _EE
                        _he_a = _hold_edge(_pmf_for(day), st_a,
                                           {m["ticker"]: m for m in meta})
                        if _he_a is not None and _he_a < _EE:
                            book.stats["argmax_churn_blocked"] += 1
                            st_a = None
                    # size against the FILL, not the quote — `_place_argmax` re-sizes
                    # once through fill_cost, and skipping that pass overstates the
                    # count (and so the stake) on every cheap leg
                    count_a = 0
                    if st_a:
                        count_a = max(1, int(1.0 / st_a.cost))
                        count_a = max(1, int(1.0 / max(st_a.fill_cost(count_a), 0.01)))
                        size_a = round(st_a.fill_cost(count_a) * count_a, 2)
                        if db_gates and _sim_risk_veto(open_rows, ev["series"], key,
                                                       size_a, opened_today):
                            book.stats["risk_vetoed"] += 1
                            st_a = None
                    realized_a = _settle_struct(st_a, count_a) if st_a else None
                    if st_a and realized_a is not None:
                        row_a = _trade_row(st_a, count_a, realized_a, "argmax")
                        if blocked_by is None:          # #147, as on the edge stream
                            opened_argmax[(ev["series"], key)] = row_a
                            # ...but charge the shared wallet only when the edge stream
                            # did NOT take this event: live holds one leg per event, so
                            # the hybrid book is what the caps see. Same rule as
                            # `_open_rows`, applied to the same day's running total.
                            if (ev["series"], key) not in opened:
                                open_rows.append({"series": ev["series"], "period": key,
                                                  "size_usd": row_a["staked"]})
                                opened_today += row_a["staked"]
                                hyb_row = row_a
            # ── stream 4: ML (research/selector.py's learned p(win)) — served HERE so
            # it carries db_gates, pit_gates, the risk veto and #142's exits, i.e. the
            # same load selector.py's own loop carries none of. See `_MLBook`. ──
            ml_row = None
            if ml_stream and (ev["series"], key) not in opened_ml and structs:
                _fit = mlbook.fit_for(day)
                if _fit is not None:
                    from prediction_market_macro.research.selector import (
                        EV_MIN as _EV_MIN, STAKE as _STAKE)
                    _sc, _clf = _fit
                    _lead = (ev["close_ts"] - day).total_seconds() / 86400.0
                    _mx = max(st.fair for st in structs)
                    best_ml = None
                    for st in structs:
                        # selector's own price window, identical to argmax's — both
                        # want payoff room net of a fee that is ceil-to-the-cent
                        if not (0.10 <= st.cost <= 0.90):
                            continue
                        _x = _MLBook.features(st, _mx, ev_spread, entropy_norm,
                                              _lead, spec.family)
                        _p = float(_clf.predict_proba(_sc.transform([_x]))[0, 1])
                        _fee1 = sum(taker_fee(l.price, 1) for l in st.legs)
                        _ev = _p - st.cost - _fee1
                        if _ev >= _EV_MIN and (best_ml is None or _ev > best_ml[0]):
                            best_ml = (_ev, _p, st)
                    if best_ml is not None and blocked_by is None:
                        _evx, _p_ml, st_m = best_ml
                        count_m = max(1, int(_STAKE / max(st_m.cost, 0.10)))
                        size_m = round(st_m.fill_cost(count_m) * count_m, 2)
                        _ml_rows = _rows_of(opened_ml.values(), day)
                        if db_gates and _sim_risk_veto(_ml_rows, ev["series"], key,
                                                       size_m, opened_today_ml):
                            book.stats["ml_risk_vetoed"] += 1
                        else:
                            realized_m = _settle_struct(st_m, count_m)
                            if realized_m is not None:
                                ml_row = _trade_row(st_m, count_m, realized_m, "ml",
                                                    shadow=True)
                                ml_row["p_win"] = round(_p_ml, 4)
                                ml_row["ev"] = round(_evx, 4)
                                opened_ml[(ev["series"], key)] = ml_row
                                opened_today_ml += ml_row["staked"]
            # ── stream 5: BLEND — per event, follow whichever line's TRAILING SETTLED
            # record is ahead, and fall through to the other when the leader has no pick.
            # The comparison uses only trades whose cash had landed BEFORE today, so this
            # is a rule that could have been run, not a hindsight pick of the winner.
            # `_MIN_SWITCH` settled ml bets are required before ml may lead, which makes
            # hybrid the default for the first stretch of any window. ──
            if ml_stream and (ev["series"], key) not in opened_blend:
                from prediction_market_macro.research.selector import (
                    MIN_SWITCH as _MIN_SWITCH)
                _mt = _trail(opened_ml.values(), day_iso)
                _bt = _trail(list(opened.values())
                             + [t for k2, t in opened_argmax.items() if k2 not in opened],
                             day_iso)
                use_ml = len(_mt) >= _MIN_SWITCH and sum(_mt) > sum(_bt)
                pick = (ml_row if use_ml else hyb_row) or (hyb_row if use_ml else ml_row)
                if pick is not None:
                    # a COPY of the row the underlying stream already built — not a second
                    # `_trade_row` call. Re-deriving it would recompute `_mtm_path` for no
                    # new information, and (when the underlying line is hybrid) would write
                    # a second, non-shadow feature row for one bet.
                    _br = dict(pick)
                    _br["used"] = "ml" if pick is ml_row else "hybrid"
                    opened_blend[(ev["series"], key)] = _br
            # ── streams 6 & 7: ARB / SNIPE census. Counts only — see `_arb_scan` for
            # why neither can be sized against candle rows. ──
            if census:
                # `spec.structure != "categorical"` is the SAME guard `decide_all` puts
                # in front of its own devig+arb block, and it is not cosmetic here: a
                # categorical book is a partition with no ordering, so reading it as a
                # survival curve manufactures "monotonicity violations" out of a
                # question that has no monotonicity to violate. Counting those would
                # make the census report opportunities the live stream is structurally
                # never shown — the exact failure this census exists to rule out.
                if spec.structure != "categorical":
                    cen["arb_event_days"] += 1
                    _sc_arb = _arb_scan(meta, _implied(meta).get("violations") or [])
                    cen["arb_violations"] += _sc_arb["n_violations"]
                    cen["arb_unpriceable"] += _sc_arb["dropped"]
                    cen["arb_would_clear"] += 1 if _sc_arb["clears"] else 0
                    if _sc_arb["best_net"] is not None and (
                            cen["arb_best_net"] is None
                            or _sc_arb["best_net"] > cen["arb_best_net"]):
                        cen["arb_best_net"] = round(_sc_arb["best_net"], 4)
                # snipe's premise is a book still OPEN after its print. Measured the same
                # way `strategy/snipe._book_shut_before_print` measures it live, off the
                # same two columns, so a zero here and a zero there mean the same thing.
                from prediction_market_macro.strategy.snipe import SNIPE_SERIES
                _in_scope = ev["series"] in SNIPE_SERIES
                cen["snipe_event_days"] += 1
                cen["snipe_event_days_in_scope"] += 1 if _in_scope else 0
                _rel_ts = release_cache.get(rk)
                if _rel_ts is None:
                    cen["snipe_no_release_ts"] += 1
                elif ev["close_ts"] > _rel_ts:
                    cen["snipe_book_open_at_print"] += 1
                    cen["snipe_open_at_print_in_scope"] += 1 if _in_scope else 0
        daily.append({"day": day.date().isoformat(), "n_opened": day_trades})

    trades = sorted(opened.values(), key=lambda t: t["settle"])
    # #142: PnL lands when the position CLOSES, which for an early exit is the exit day,
    # not the event's settlement date. Ordering the curve by settle while booking an
    # exit's PnL would put cash on the timeline days after it actually arrived.
    curve, run_pnl = [], 0.0
    for t in sorted(trades, key=lambda x: (x.get("exit_day") or x["settle"])):
        run_pnl += t["realized"]
        curve.append({"day": t.get("exit_day") or t["settle"], "pnl": round(run_pnl, 4)})
    by_series: dict[str, dict] = {}
    for t in trades:
        b = by_series.setdefault(t["series"], {"n": 0, "won": 0, "staked": 0.0,
                                               "realized": 0.0})
        b["n"] += 1
        b["won"] += 1 if t["won"] else 0
        b["staked"] = round(b["staked"] + t["staked"], 4)
        b["realized"] = round(b["realized"] + t["realized"], 4)
    lead_buckets: dict[str, dict] = {}
    for t in trades:
        k = ("0-1d" if t["lead_days"] <= 1 else "1-3d" if t["lead_days"] <= 3
             else "3-7d" if t["lead_days"] <= 7 else ">7d")
        b = lead_buckets.setdefault(k, {"n": 0, "won": 0, "realized": 0.0})
        b["n"] += 1
        b["won"] += 1 if t["won"] else 0
        b["realized"] = round(b["realized"] + t["realized"], 4)
    tot_staked = sum(t["staked"] for t in trades)
    tot_real = sum(t["realized"] for t in trades)

    def _stream_summary(ts_list):
        stk = sum(x["staked"] for x in ts_list)
        rl = sum(x["realized"] for x in ts_list)
        return {"n_trades": len(ts_list),
                "won": sum(1 for x in ts_list if x["won"]),
                "win_rate": round(sum(1 for x in ts_list if x["won"])
                                  / len(ts_list), 4) if ts_list else None,
                "staked": round(stk, 4), "realized": round(rl, 4),
                "roi": round(rl / stk, 5) if stk > 0 else None,
                # NOT capped. `freeze_track` derives the displayed history from this list
                # and recomputes every aggregate from it, so a cap here would export a
                # self-consistent but silently partial public track record. The old
                # [-60:] was invisible only because a 60-day run stays under it; a
                # 6-month one does not. Size is bounded by the window, and a full
                # 7-month run is ~150 trades — small next to the ladder JSON already
                # stored per run.
                "trades": sorted(ts_list, key=lambda x: x["settle"])}

    def _held_analysis(ts_list) -> dict:
        """§25.5 — "was it green for a while and we didn't get out?", quantified.

        Three numbers, and the third is the one that matters:

          `n_observed`     trades with at least one daily mark between entry and close.
                           Everything below is a fraction OF THIS, not of all trades —
                           a trade opened the day before close simply has no path, and
                           counting it as "never green" would answer the question with
                           an artefact of the entry timing.
          `n_gave_back`    trades whose best daily BID-side mark beat what they settled
                           for. These are the ones the question is about.
          `oracle_gain`    what a perfect exit at each trade's own peak would have added
                           over settling. This is an UPPER BOUND that no rule can reach:
                           it picks the peak with hindsight, one observation per day, at
                           a candle close we did not necessarily see. Read it as "is
                           there even anything here to chase" — if it is small, no exit
                           rule can help and the answer to the user's question is no.

        `oracle_gain_mid` is the same bound priced at midpoints. The spread between the
        two is the part of any apparent profit that the book would not actually have
        paid; on a wide book it is most of it.
        """
        obs = [t for t in ts_list if t.get("mtm_days")]
        if not obs:
            return {"n_observed": 0, "note": "no trade had a daily mark before close"}
        gave_back = [t for t in obs if t["mtm_peak"] > t["realized"]]
        ever_green = [t for t in obs if t["mtm_peak"] > 0]
        gain = sum(max(t["mtm_peak"], t["realized"]) - t["realized"] for t in obs)
        gain_mid = sum(max(t["mtm_peak_mid"], t["realized"]) - t["realized"] for t in obs)
        rl = sum(t["realized"] for t in obs)
        stk = sum(t["staked"] for t in obs)
        return {"n_trades": len(ts_list), "n_observed": len(obs),
                "n_ever_green_at_bid": len(ever_green),
                "n_gave_back": len(gave_back),
                # #142: with the live exit rules modelled, `realized` is the EXIT price
                # on these and the path stops there — so the oracle below is now "the
                # best day we could have picked while we still held it", a tighter and
                # more honest bound than the old entry-to-settlement version.
                "n_exited_early": sum(1 for t in ts_list if t.get("exit_day")),
                "realized": round(rl, 4), "staked": round(stk, 4),
                "roi": round(rl / stk, 5) if stk > 0 else None,
                "oracle_gain": round(gain, 4),
                "oracle_gain_mid": round(gain_mid, 4),
                "oracle_roi": round((rl + gain) / stk, 5) if stk > 0 else None,
                "note": "oracle = hindsight exit at each trade's own peak daily mark;"
                        " an UPPER BOUND, not an achievable rule (§25.5)"}

    argmax_trades = list(opened_argmax.values())
    # hybrid = WC live rule: edge bet where it fired, favourite where it passed
    hybrid = list(trades) + [t for k2, t in opened_argmax.items()
                             if k2 not in opened]
    ml_trades = sorted(opened_ml.values(), key=lambda t: t["settle"])
    blend_trades = sorted(opened_blend.values(), key=lambda t: t["settle"])
    streams = {"edge": _stream_summary(trades),
               "argmax": _stream_summary(argmax_trades),
               "hybrid": _stream_summary(hybrid)}
    if ml_stream:
        streams["ml"] = _stream_summary(ml_trades)
        streams["blend"] = _stream_summary(blend_trades)
    held = {k: _held_analysis(v) for k, v in
            (("edge", trades), ("argmax", argmax_trades), ("hybrid", hybrid))}
    if census:
        # `arb` and `snipe` get an entry in `streams` too, so the table has SIX rows and
        # a reader cannot mistake "absent" for "found nothing". They carry n_trades=0 by
        # construction, and `census` says why — a stream that cannot be sized here must
        # not be given a PnL that looks sized.
        streams["arb"] = {"n_trades": 0, "won": 0, "win_rate": None, "staked": 0.0,
                          "realized": 0.0, "roi": None,
                          "census": {"event_days": cen["arb_event_days"],
                                     "violations": cen["arb_violations"],
                                     "unpriceable": cen["arb_unpriceable"],
                                     "would_clear_fee_gate": cen["arb_would_clear"],
                                     "best_net_per_contract": cen["arb_best_net"]},
                          "note": "not sizeable in simulation — candle rows carry no"
                                  " depth, so 铁律 5 cannot bind. Counts only."}
        streams["snipe"] = {"n_trades": 0, "won": 0, "win_rate": None, "staked": 0.0,
                            "realized": 0.0, "roi": None,
                            "census": {"event_days": cen["snipe_event_days"],
                                       "book_open_at_print":
                                           cen["snipe_book_open_at_print"],
                                       "event_days_in_scope":
                                           cen["snipe_event_days_in_scope"],
                                       "open_at_print_in_scope":
                                           cen["snipe_open_at_print_in_scope"],
                                       "no_scheduled_release":
                                           cen["snipe_no_release_ts"]},
                            "note": "§24-B needs a book still open AFTER its print."
                                    " `*_in_scope` counts only SNIPE_SERIES, which is"
                                    " what the live stream watches; the wider count"
                                    " includes books it is not pointed at."}
    out = {"days": days, "fair_mode": fair_mode, "streams": streams,
           "census": cen if census or ml_stream else None,
           "held_analysis": held, "held_paths": held_paths,
           "feature_rows": feature_rows,
           "window_start": (sim_end - timedelta(days=days)).date().isoformat(),
           "window_end": sim_end.date().isoformat(),
           "bounded": end is not None,
           "n_trades": len(trades),
           "win_rate": round(sum(1 for t in trades if t["won"]) / len(trades), 4)
           if trades else None,
           "staked": round(tot_staked, 4), "realized": round(tot_real, 4),
           "roi": round(tot_real / tot_staked, 5) if tot_staked > 0 else None,
           "by_series": by_series, "lead_buckets": lead_buckets,
           "curve": curve[-60:], "trades": trades[-60:],
           "db_gates": db_gates, "select_params": select_params,
           "gate_params": gate_params,
           "select_mode": select_mode,
           "argmax_filter": argmax_filter, "model_exits": model_exits,
           "gate_stats": dict(book.stats),
           "note": ("db-state gates ON, rebuilt per simulated day from strictly-earlier"
                    " closes (research/pit_gates); params from the daily "
                    + ("raw-argmin selector (research/param_argmin, the production"
                       " selector since 2026-08-11), PIT via matrix masking"
                       if select_mode == "argmin" else "selector")
                    + "; ops/exits.py rules 1+3 modelled daily (#142)"
                    if db_gates and select_params and argmax_filter and model_exits else
                    f"db_gates={db_gates} select_params={select_params}"
                    f" argmax_filter={argmax_filter} model_exits={model_exits}"
                    " — diagnostic run, NOT the live rule")}
    # The gate/param suffix keeps an aligned run from overwriting a pure-gate diagnostic
    # of the same window — they are different strategies. Built ONCE and shared by both
    # rows below: it used to be spelled out twice, and two copies of a key that must match
    # is a drift waiting to happen (a suffix added to one and not the other would split the
    # headline from its own feature rows).
    cfg_hash = (f"d{days}:{fair_mode}:end{sim_end.date().isoformat()}"
                f"{'' if db_gates and select_params else ':pure'}"
                f"{'' if argmax_filter else ':nofav'}"
                f"{'' if model_exits else ':noexit'}"
                # #147: a shadow-blocked run carries EXTRA feature rows (placed=False)
                # while its PnL is identical BY CONSTRUCTION — which is exactly why a
                # collision here would be invisible. The headline would still read
                # correctly while the stored training set had silently changed shape.
                f"{':shadowblocked' if shadow_blocked else ''}"
                f"{':argminsel' if select_mode == 'argmin' else ''}"
                # 2026-08-20: the shadow streams are additive and PnL-neutral, so a run
                # WITHOUT them would otherwise collide with the default run and replace a
                # six-stream row with a three-stream one. The suffix goes on the poorer
                # payload, leaving the canonical hash owned by the complete table.
                f"{'' if (ml_stream and census) else ':noshadow'}"
                f"{':' + hash_tag if hash_tag else ''}")
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('daily_walkforward',?,'*',?,?,?)",
        # `window` MUST keep the "{days}d:" prefix — frontend_export filters the
        # daily/daily60 headline rows with window LIKE '30d%' / '60d%'. The dated
        # window lives in metrics_json (window_start/window_end) instead.
        (cfg_hash,
         f"{days}d:{fair_mode}",
         # `held_paths`/`feature_rows` are returned to the caller but NOT written into
         # the headline row. That row is read by `frontend_export` and copied by
         # `freeze_track`; the two diagnostics are per-day and per-trade arrays that would
         # multiply its size for readers that never look at them. They go to their own row
         # below, under the same config_hash, so the training set stays addressable
         # without re-running the 50-minute sim.
         json.dumps({k: v for k, v in out.items()
                     if k not in ("held_paths", "feature_rows")},
                    ensure_ascii=False), now.isoformat()))
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('walkforward_features',?,'*',?,?,?)",
        (cfg_hash,
         f"{days}d:{fair_mode}",
         json.dumps({"feature_rows": feature_rows, "held_paths": held_paths,
                     "window_start": out["window_start"],
                     "window_end": out["window_end"]}, ensure_ascii=False),
         now.isoformat()))
    conn.commit()
    return out


def coverage(conn, days: int = 30) -> dict:
    """Which registered bets the window can even test: settled events with candled
    legs per series, plus the registered-but-untestable ones (no settle in window).

    `cd.end_ts > 0` is load-bearing. ingest/kalshi_md.py records a NULL-price row at
    end_ts=0 when Kalshi 404s the candlestick endpoint, so that backfill stops retrying
    a leg that will never have bars. 6700 of the 14683 candle rows are that sentinel, and
    they belong exclusively to tickers with no real bars at all — joining on them counted
    a leg as testable precisely when it is not. Over the full settled book that read 61
    testable periods for KXCPI where 2 have prices, and 129 vs 10 for KXAAAGASW. The price
    readers were never fooled (they all reject NULL bid/ask), so this was a reporting
    error only, but it is the one that made replay n=1..11 look inexplicable.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    out = {}
    for spec in REGISTRY.values():
        r = conn.execute(
            "SELECT COUNT(DISTINCT s.period) n FROM settlements s"
            " JOIN contracts c ON c.ticker=s.ticker"
            " JOIN candles cd ON cd.ticker=s.ticker AND cd.end_ts>0"
            " WHERE s.series=? AND s.result IN ('yes','no') AND s.settled_ts>=?",
            (spec.ticker, since.isoformat())).fetchone()
        out[spec.ticker] = {"family": spec.family, "cadence": spec.cadence,
                            "events_in_window": r["n"],
                            "testable": r["n"] > 0,
                            "in_dispatch": spec.ticker in SERIES_DISPATCH}
    return out


def sweep(conn, days: int = 30, leads=(1.0, 3.0, 5.0, 7.0)) -> dict:
    """Full WF per entry-lead: at lead L an entry opens the first day within L
    days of close (fresher data, different prediction, different price)."""
    now = datetime.now(timezone.utc)
    per_lead = {}
    for lead in leads:
        r = run(conn, days=days, max_lead_days=lead)
        per_lead[f"{lead:g}d"] = {k: r.get(k) for k in
                                  ("n_trades", "win_rate", "staked", "realized",
                                   "roi", "by_series", "curve")}
        # The keys above are `run`'s headline, which is the EDGE stream alone
        # (`trades = opened.values()`; the favourite leg lives in `opened_argmax`).
        # Production trades the HYBRID rule — `decide_all` places the favourite
        # exactly when the edge decision passed — so choosing an entry lead by the
        # headline ROI optimises a stream nobody trades. It flatters, consistently:
        # on the stored 30d/60d runs the argmax leg returns +17.5%/-0.3% against
        # edge's +153.6%/+36.7%, which is what drags hybrid down to +114.7%/+31.5%.
        # Carried alongside rather than replacing the headline: `freeze_track` and
        # the sweep chart read the existing keys, and a lead sweep is worth reading
        # per stream anyway — the favourite's best lead need not be the edge's.
        # Trade lists dropped; four leads x three streams of them is payload the
        # sweep artifact never reads, and `run` already stores the full ones.
        per_lead[f"{lead:g}d"]["streams"] = {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "trades"}
            for k, v in (r.get("streams") or {}).items()}
    cov = coverage(conn, days)
    out = {"days": days, "leads": per_lead, "coverage": cov,
           "generated_at": now.isoformat(),
           "note": "one full PIT walk-forward per lead; per-series cells have tiny"
                   " n — read the GLOBAL row, treat per-series as anecdotes"}
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('walkforward_sweep',?,'*',?,?,?)",
        (f"d{days}:model:{now.date().isoformat()}", f"{days}d:model",
         json.dumps(out, ensure_ascii=False), now.isoformat()))
    conn.commit()
    return out


def main():
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--pooled", action="store_true")
    ap.add_argument("--end", help="last simulated day YYYY-MM-DD (default: today)."
                                  " Required when the run backs a displayed track"
                                  " segment, so it cannot overlap the live ledger.")
    ap.add_argument("--pure-gates", action="store_true",
                    help="skip the db-state gates (skill/calibration/capture/conformal)."
                         " Diagnostic only — the result is NOT the strategy production"
                         " runs, so it must not back a displayed segment.")
    ap.add_argument("--fixed-params", action="store_true",
                    help="pin models to the registered defaults instead of asking the"
                         " daily selector. Diagnostic only, same caveat.")
    ap.add_argument("--no-argmax-filter", action="store_true",
                    help="drop the favourite leg's defer-to-market rule (fair <= cost)."
                         " Diagnostic: it re-measures a rule whose 27W-2L validation"
                         " predates the PIT gate rebuild. Production keeps the rule.")
    ap.add_argument("--no-model-exits", action="store_true",
                    help="hold every position to settlement instead of running"
                         " ops/exits.py's rules daily (#142). Diagnostic ONLY: the live"
                         " pipeline always calls exits.run, so this measures a strategy"
                         " production does not run. Use it to A/B the exit rules, never"
                         " to produce a displayed number.")
    ap.add_argument("--no-gate-params", action="store_true",
                    help="build the PIT gate history at the registered defaults instead"
                         " of at the per-day selected params (#199). Diagnostic ONLY:"
                         " with --select-params on, this is the pre-#199 pairing where"
                         " predictions used one config and the skill/capture/conformal"
                         " state described another. Use it to A/B, never to produce a"
                         " displayed number.")
    ap.add_argument("--shadow-blocked", action="store_true",
                    help="also emit §25.3 feature rows for gate-aborted events"
                         " (placed=False, blocked_by=<gate>), so ser_roi/skill_ratio are"
                         " observable on BOTH sides of the threshold that blocks them"
                         " (#147/§25.17). PnL-neutral: blocked rows never enter trades."
                         " Research only — it makes the column readable, it does not"
                         " validate §25.4, and a verdict still needs a forward window.")
    ap.add_argument("--select-argmin", action="store_true",
                    help="replay the 2026-08-11 production selector (daily raw argmin,"
                         " research/param_argmin) instead of the DSR-gated one. PIT via"
                         " matrix masking; this IS the live rule since the policy"
                         " change, so runs backing a displayed segment should use it.")
    ap.add_argument("--no-shadow-streams", action="store_true",
                    help="drop the ml/blend/arb/snipe counterfactual lines and report"
                         " only edge/argmax/hybrid. They are PnL-neutral for those three"
                         " by construction, so this is a SPEED knob (it skips the"
                         " per-day selector refit and the per-event arb scan), not an"
                         " A/B — use it when only the live rule's number is wanted.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    end = None
    if args.end:
        end = datetime.fromisoformat(args.end).replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc)
    s = load_settings()
    conn = init_db(s.db_path)
    if args.sweep:
        out = sweep(conn, days=args.days)
        slim = {"leads": {k: {kk: v[kk] for kk in ("n_trades", "win_rate",
                                                   "realized", "roi")}
                          for k, v in out["leads"].items()},
                "coverage": {k: v["events_in_window"]
                             for k, v in out["coverage"].items()}}
        print(json.dumps(slim, indent=1, ensure_ascii=False))
        return
    out = run(conn, days=args.days, end=end,
              fair_mode="pooled" if args.pooled else "model",
              db_gates=not args.pure_gates, select_params=not args.fixed_params,
              argmax_filter=not args.no_argmax_filter,
              model_exits=not args.no_model_exits,
              gate_params=not args.no_gate_params,
              shadow_blocked=args.shadow_blocked,
              select_mode="argmin" if args.select_argmin else "dsr",
              ml_stream=not args.no_shadow_streams,
              census=not args.no_shadow_streams,
              log=print if args.verbose else None)
    print(json.dumps({k: v for k, v in out.items() if k not in ("trades", "curve")},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
