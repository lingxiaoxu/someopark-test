"""research/pit_gates.py — the four db-state gates, recomputed as of any simulated day.

`walkforward` shipped with this note: "db-state gates (calibration map, capture memory,
skill ratio, conformal) are deliberately EXCLUDED: they were fit on data that includes
this window; using them here would be self-referential leakage". The objection was
correct about the STORED artefacts and wrong as a reason to drop the gates — it left the
backtest running a strategy the live path does not run, which is the worse error, because
the two halves of the displayed track record then measure different things.

The fix is not to use the stored artefacts and not to skip the gates, but to REBUILD each
one at every simulated day from events that closed strictly earlier. That is exactly what
the live path had available on that day, so there is no leakage and no mismatch.

**What each gate is rebuilt from, and what it actually does over 2026-06/07.**

  skill        Paired (model, market) Brier per event, trailing 20, ratio > 1.05 ⇒
               defensive (double the edge bar, halve size), > 1.50 ⇒ blocked outright.
               This is the one with real bite: on the live ledger it accounts for 27
               passes, all KXJOBLESSCLAIMS and KXAAAGASW. Note `decide_all` `continue`s
               on a block, which suppresses the argmax leg too — replicated here, because
               a hybrid that keeps betting favourites on a blocked series is not the live
               rule.

  calibration  PINNED TO IDENTITY on both sides since 2026-08-10 (see GateHistory.asof
               and strategy/calibration.py): the isotonic maps fit on event-clustered
               per-leg pairs produced 0/1-certainty plateaus and base-rate shelves, and
               live decision #4346 bought a raw-fair-0.334 leg as fair 0.60 off one.
               The map machinery stays for research (shadow row); no fair is modified.

  conformal    ACI on the model's own per-event Brier sequence; a breach halves
               `max_size_usd`. Sizing only — it cannot open or close a position.

  capture      Per-strike realised/expected memory, drops a strike below 40% capture with
               n >= 8. Measured inert: zero blocking cap_keys on all 15 series even on
               the FULL history, so a PIT slice of it cannot block anything either. It is
               wired anyway and the drop count is reported — an inert gate that is present
               and counted is evidence; an inert gate that is assumed is a guess.

  series_      §25.4's per-series switch, folded over the same `decision_replay` trades
  enable       the capture memory is built from: trailing-12 realised ROI <= 0 disables
               the series, > +2.6% re-enables it. Like `skill`, a disable aborts the
               event including the argmax leg. Costs nothing extra — the trade list was
               already being loaded for capture.

**Cost.** Each series is replayed twice, once (`replay_series`) for the Brier/leg pairs
and once (`decision_replay`) for the capture trades, and then the per-day state is a slice
of those two lists. So the whole 60-day sim pays two replays per series, not 60. The
sliced state only changes when an event closes, so `asof` memoises on the event count.
"""
from __future__ import annotations

from datetime import datetime

from prediction_market_macro.strategy import capture as _capture
from prediction_market_macro.strategy import conformal as _conformal
from prediction_market_macro.strategy import series_enable as _series_enable
from prediction_market_macro.strategy import skill as _skill
from prediction_market_macro.strategy.calibration import interp


def period_closes(conn, series: str) -> dict[str, datetime]:
    """{period key: close_ts} for every settled event of the series.

    The replays key their records by the model's period KEY while `contracts` stores
    Kalshi's token, so this is the join that lets a record be placed on the timeline at
    all. A record whose token does not map is dropped rather than treated as closing at
    epoch — an unplaceable event must not leak into every as-of slice.
    """
    from prediction_market_macro.util.periods import kalshi_period_to_key
    out: dict[str, datetime] = {}
    for r in conn.execute(
            "SELECT s.period, MAX(c.close_time) ct FROM settlements s"
            " JOIN contracts c ON c.ticker=s.ticker WHERE s.series=?"
            " AND s.result IN ('yes','no') GROUP BY s.period", (series,)):
        key = kalshi_period_to_key(r["period"])
        if not key or not r["ct"]:
            continue
        out[key] = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
    return out


class GateState:
    """The five gates as they stood on one simulated day. Pure data + pure application."""

    __slots__ = ("series", "n_events", "cal_xy", "capture", "skill_ratio",
                 "conformal_factor", "enable_state")

    def __init__(self, series, n_events, cal_xy, capture, skill_ratio, conformal_factor,
                 enable_state=None):
        self.series = series
        self.n_events = n_events
        self.cal_xy = cal_xy                      # (xs, ys) or None ⇒ identity
        self.capture = capture                    # {cap_key: {n, expected, realized}}
        self.skill_ratio = skill_ratio            # float or None
        self.conformal_factor = conformal_factor  # 1.0 or BREACH_FACTOR
        self.enable_state = enable_state or {"enabled": True}   # §25.4 fold

    # ── the things decide_all does with this state ──────────────────────────────

    @property
    def blocked(self) -> float | None:
        r = self.skill_ratio
        return r if (r is not None and r > _skill.BLOCK_RATIO) else None

    @property
    def disabled(self) -> str | None:
        """§25.4 per-series switch: the reason this series may not trade, or None.

        Same shape and same consequence as `blocked` — the caller aborts the event,
        argmax leg included. Kept as a separate property rather than folded into
        `blocked` because the two answer different questions (is the FORECAST behind the
        market, vs do the BETS lose money) and a merged reason string would make the
        ledger unable to say which one fired.

        #155: `veto`, not `reason` — the same call `ops/decide_all` makes, so
        `series_enable.SHADOW` reaches both lanes from one switch. While it is on this is
        always None and the harness trades the series the gate dislikes, which is what
        production does. `would_disable` below is the observation.
        """
        return _series_enable.veto(self.enable_state)

    @property
    def would_disable(self) -> str | None:
        """What §25.4 says regardless of `SHADOW`. Report it; never branch trading on it."""
        return _series_enable.reason(self.enable_state)

    @property
    def defensive(self) -> float | None:
        r = self.skill_ratio
        return r if (r is not None and r > _skill.RATIO_MAX) else None

    def calibrate_structs(self, structs: list) -> list:
        """`calibration.calibrate_structs` against the PIT map instead of the stored one.

        Same `interp` the live path uses, so a map that produces identical breakpoints
        produces identical fairs — the difference between backtest and live here is the
        SAMPLE the map was fit on, never the arithmetic.
        """
        if not self.cal_xy:
            return structs
        from dataclasses import replace
        entry = (None, self.cal_xy[0], self.cal_xy[1])
        out = []
        for st in structs:
            cf = round(interp(entry, st.fair), 6)
            out.append(replace(st, fair=cf) if cf != st.fair else st)
        return out

    def filter_capture(self, structs, model_mean, round_rule, strikes) -> tuple[list, list]:
        if not self.capture or model_mean is None:
            return structs, []
        return _capture.filter_structs(structs, self.capture, model_mean, round_rule,
                                       strikes)

    def apply_gates(self, gates: dict) -> dict:
        """The size/bar adjustments, in `decide_all`'s order: conformal throttle first,
        then the defensive skill widening. `blocked` is NOT handled here — it aborts the
        event before `decide()` is reached, argmax leg included."""
        g = dict(gates)
        g["max_size_usd"] *= self.conformal_factor
        if self.defensive is not None:
            g["min_net_edge"] *= 2.0
            g["max_size_usd"] *= 0.5
            g["fav_min_edge_per_day"] = g.get("fav_min_edge_per_day", 0.008) * 2.0
        return g


class GateHistory:
    """Two replays of one series, sliceable to any as-of day.

    `params_pit` (#198) carries the same fix the §9.5 gate got — replay each event at the
    parameters in force at its own asof instead of at the registered defaults — and it
    DEFAULTS OFF here on purpose, which is the one place in that ticket where the honest
    answer is "not yet".

    The reason is a coupling, not a doubt about the fix. This class is driven by
    `walkforward._GateBook`, whose PREDICTION path already chooses params per simulated
    day through `params_for()` — honouring `select_params`, `select_mode='argmin'` and
    `param_override`. Reading the real adoption history here regardless would pair a
    defaults-predicted (or argmin-replayed, or overridden) sim with a gate state built
    from what production actually adopted, which is a THIRD combination that never ran.

    `params_at` (#199) is that thread, and it is what `_GateBook` now passes: a callable
    `asof -> params|None` returning the choice of the selector UNDER TEST. It supersedes
    `params_pit` for simulation callers, because "what production adopted" and "what this
    sim's selector would choose" are the same answer only in the one case where the sim
    is replaying the live lane. `params_pit` stays for `eval.run_series`, whose question
    genuinely IS the adoption history.

    NOT wired into `pnl_score.gate_history`, deliberately and for the reason written
    there: that history is held at the incumbent defaults so a 73-set grid does not
    become 73 replays, and so the gates are not fit on the very predictions being scored.
    Making it per-candidate there would be circular. Here it is not — the sim's params
    come from a selector whose own scoring runs against that defaults-pinned history, so
    the dependency is one-way and terminates.
    """

    def __init__(self, conn, series: str, max_events: int = 200,
                 params_pit: bool = False, params_at=None):
        from prediction_market_macro.research.backtest import replay_series
        from prediction_market_macro.research.eval import decision_replay
        self.series = series
        self.closes = period_closes(conn, series)
        per = replay_series(conn, series, max_events=max_events, collect_legs=True,
                            params_pit=params_pit, params_at=params_at)["per_release"]
        # (close_ts, record) chronological — replay_series already returns chronological
        # order but sorting on the close we just resolved makes that independent of it.
        self.per = sorted(((self.closes[p["period"]], p) for p in per
                           if p["period"] in self.closes), key=lambda t: t[0])
        dec = decision_replay(conn, series, max_events=max_events, collect_trades=True,
                              params_pit=params_pit, params_at=params_at)
        self.dec = sorted(((self.closes[t["period"]], t) for t in dec["trades"]
                           if t["period"] in self.closes), key=lambda t: t[0])
        self._cache: dict[int, GateState] = {}

    def asof(self, day: datetime) -> GateState:
        """State built from events that CLOSED strictly before `day`.

        Strictly before, not at-or-before: an event closing at 16:00 on the simulated day
        has not settled when the 16:00 decision is taken, and letting its own outcome into
        the calibration map or the skill ratio is precisely the leakage this module exists
        to avoid.
        """
        # Memoised on the count of closed events, which is a valid key for the capture
        # slice too: every `dec` trade belongs to one of these events, so no trade can
        # cross the boundary on a day when no event does.
        n = sum(1 for c, _ in self.per if c < day)
        st = self._cache.get(n)
        if st is not None:
            return st
        recs = [r for c, r in self.per if c < day]

        # Calibration PINNED TO IDENTITY (2026-08-10), mirroring the live path — see
        # strategy/calibration.py's module docstring for the incident. The per-leg
        # pairs here are clustered by event exactly like the live refit's were, so the
        # PIT-fit maps this used to build had the same cliff/certainty pathology; the
        # backtest and the ledger have to be the same strategy, and that strategy no
        # longer applies a map. Un-pinning here without un-pinning live (or vice
        # versa) recreates the #128-class divergence this module exists to prevent.
        cal_xy = None

        paired = [(r["brier_model-1h"], r["brier_market-1h"]) for r in recs
                  if r.get("brier_model-1h") is not None
                  and r.get("brier_market-1h") is not None]
        # trailing window taken from the END: `skill.skill_ratio` slices the most recent
        # TRAIL paired periods, and "recent" here is by close date, which is the order
        # `self.per` is in. The live query orders by the period STRING, which agrees for
        # every period format in the registry ('2026-07', '2026-07-31').
        paired = paired[-_skill.TRAIL:]
        ratio = None
        if len(paired) >= _skill.MIN_PAIRED:
            mk = sum(p[1] for p in paired) / len(paired)
            if mk > 1e-9:
                ratio = (sum(p[0] for p in paired) / len(paired)) / mk

        scores = [r["brier_model-1h"] for r in recs
                  if r.get("brier_model-1h") is not None]
        factor = _conformal.evaluate_sequence(scores)["factor"]

        caps: dict[str, dict] = {}
        earlier = []
        for c, t in self.dec:
            if c >= day:
                continue
            earlier.append(t)
            e = caps.setdefault(t["cap_key"], {"n": 0, "expected": 0.0, "realized": 0.0})
            e["n"] += 1
            e["expected"] = round(e["expected"] + t["expected"], 4)
            e["realized"] = round(e["realized"] + t["realized"], 4)

        # §25.4: the per-series switch is a fold over the SAME strictly-earlier trades
        # the capture memory is built from. `self.dec` is already in close order, so
        # `earlier` is the chronological prefix the fold's hysteresis needs.
        se = _series_enable.evaluate(earlier)

        st = GateState(self.series, n, cal_xy, caps, ratio, factor, se)
        self._cache[n] = st
        return st
