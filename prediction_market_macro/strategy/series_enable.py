"""strategy/series_enable.py — §25.4: stop betting a series that does not pay.

**Why this exists.** On the 75-day hybrid window (2026-05-21 .. 2026-08-04) the loss was
not spread across the book, it was five series:

    KXJOBLESSCLAIMS   6 trades   17% win   -87.7% ROI
    KXCPIYOY          2 trades    0% win   ~-105%
    KXU3              2 trades    0% win   ~-105%
    KXPAYROLLS        2 trades    0% win   ~-105%
    KXCPICORE         2 trades    0% win   ~-105%

(Counts as of the 2026-08-05 freeze, 51 trades / -$9.61: eight carried **86%** of the loss.
Re-frozen 2026-08-07 after the F1-F9 parity fixes, #148 and #155 the same window is 41
trades / -$9.13, and KXJOBLESSCLAIMS alone is -$4.94 — 54% of the loss on 6 of 41 trades.

**Read that as description, not as justification.** #146 took this concentration to LOEO
and `ser_roi` clipped to zero in all 18 folds: which series lost money in-sample does NOT
predict which loses money out-of-fold. So the module below is deliberately NOT a fitted
concentration model — it is a pure hysteresis rule on a series' own trailing ROI, with
both thresholds pinned to costs rather than to anything estimated from this loss table,
and it ships in SHADOW. The table is why anyone looked; it is not why the rule is trusted.)

Nothing on the paper decision path can
switch a series off: `experiments.series_gate` (§9.5) exists but is read by exactly one
caller, `exec/kalshi_exec.py:48`, and that is the REAL-MONEY promotion gate. The paper
path — the path that produces the displayed track — has never had a per-series veto.

**What the signal is, and why it is not the live ledger.** The state is folded over the
series' own `eval.decision_replay` trades: a candle-rebuilt replay that opens on the first
day the production gates clear and settles against the real result. Two properties make it
the right source and the live ledger the wrong one:

  1. It is PIT-sliceable. `pit_gates.GateHistory` already loads exactly this list to
     rebuild the capture memory, so the backtest can evaluate this gate as of any
     simulated day from events that closed strictly earlier — no leakage, and the
     backtest runs the same rule the live path runs.
  2. **It keeps accruing while the series is switched off.** A gate fed by realised live
     bets would be absorbing: disable the series, no more bets, no more evidence, never
     re-enabled. The replay does not need us to have traded, so a disabled series keeps
     generating the evidence that can turn it back on. This is the single most important
     design property here and it must survive any future rewrite.

**It can only ever subtract.** `blocked()` returns a reason or None. There is no path by
which this module lets a trade through that another gate stopped — in particular it must
never become a way around the skill block (#124). Callers apply it as one more veto in the
same place `decide_all` handles `skill.blocked`, and like that one it aborts the whole
event including the argmax leg: a hybrid that keeps buying favourites on a series we have
declared unprofitable is not a coherent rule.

**The constants are borrowed, not fitted.** Tuning a threshold until the 75-day window
looks good is a guaranteed overfit (§25.1), so every number here is either zero or lifted
from a constant that already existed:

  * `OFF_ROI = 0.0` — breakeven. The one non-arbitrary point on the axis. A series whose
    trailing realised dollars are negative is not paying for itself; that is the whole
    claim, and it has no free parameter.
  * `ON_ROI = 0.026` — re-enable needs trailing ROI above one round-trip net taker cost,
    not merely above zero. This is the hysteresis band; without it a series sitting at
    -0.1% flips on and off on every event. 2.6% is the measured net Kalshi taker drag on
    this book (§25.3), not a tuned figure.
  * `MIN_N = 6` — `skill.MIN_PAIRED`. Below this the gate abstains and the series trades.
  * `WINDOW = 12` — `dsr.MIN_OBS`. The trailing window the ROI is measured over.

**The known cost: turnover.** `WINDOW = 12` on a weekly series is ~3 months to fully turn
over, so a series switched off on a bad run stays off for a while even after it improves.
That is #124's exact pathology on a different gate, and it is a deliberate trade: a shorter
window is noisier and a noisy per-series switch is worse than a sticky one, because it
flips off precisely the series that just had a losing streak by chance. It is recorded here
rather than tuned away. If the measured fire-rate on the window turns out to be near zero
or near one, the answer is to report that, NOT to move these numbers.

**No storage.** The state machine is a pure fold over the chronological trade list, so the
live path and the PIT path compute the same answer from the same list without a table to
keep in sync. Hysteresis makes the result path-dependent, which is why it is a fold over
the whole prefix rather than a test on the last window.
"""
from __future__ import annotations

WINDOW = 12          # research.dsr.MIN_OBS — trailing trades the ROI is measured over
MIN_N = 6            # strategy.skill.MIN_PAIRED — below this the gate abstains
OFF_ROI = 0.0        # disable at or below breakeven
ON_ROI = 0.026       # re-enable only above one round-trip net taker cost (§25.3)

SHADOW = True
"""#155. Compute the verdict, record it, act on NOTHING.

**This is a switch, not a design.** Flip it to False and §25.4 becomes a live veto in the
same instant in both lanes — `veto()` is the one function that reads it, and both
`ops/decide_all` (live) and `research/pit_gates.GateState.disabled` (backtest) go through
it. There is deliberately no second place to change and no per-lane override: a gate the
backtest applies and the live path does not is exactly the #109/#128/#151 divergence, and
it has already happened once on THIS gate (see `blocked` below).

**Why it is on.** The gate would currently switch off KXWTIW (roi -0.27462, n=6) and
KXJOBLESSCLAIMS (roi -0.2895, n=10). Both are inside the sampling frame of two live
pre-registered tests — PR-2 (#126, the argmax defer-to-market arm) and PR-7 step 1 (#143,
the S2 exit arm). Silently removing two series mid-flight does not make those tests
conservative, it makes them tests of a different population than the one registered, and
the registration cannot then be read out at all. Turning the gate on is a decision to be
taken once PR-2 reaches n=20, not a side effect of the weekly eval job finally running.

**What "shadow" buys.** The verdict is written to `shadow_series_enable` once per series
per day whether it blocks or not, so when the switch is flipped the question "what would
this have cost us" is answered from a record rather than from a reconstruction — the same
argument `exits.shadow_run` makes at length, and for the same reason: choosing the
comparison after the settlements are known is how a forward test becomes a fitted one.

Flipping this re-opens nothing else. The thresholds above are untouched by it and must
stay untouched — §25.1.
"""


def _roi(window: list[dict]) -> float | None:
    staked = sum(float(t["staked"]) for t in window)
    if staked <= 0:
        return None
    return sum(float(t["realized"]) for t in window) / staked


def evaluate(trades: list[dict]) -> dict:
    """Fold the state machine over `trades` (chronological, each {staked, realized}).

    Returns {enabled, n, roi, flips} where `roi` is the trailing window as it stands
    after the last trade — the number the next decision would be taken on. `n` is the
    size of that window, not the length of the whole history.
    """
    enabled, flips = True, 0
    roi = None
    n = 0
    for i in range(len(trades)):
        w = trades[max(0, i + 1 - WINDOW):i + 1]
        if len(w) < MIN_N:
            continue
        r = _roi(w)
        if r is None:
            continue
        roi, n = r, len(w)
        if enabled and r <= OFF_ROI:
            enabled, flips = False, flips + 1
        elif not enabled and r > ON_ROI:
            enabled, flips = True, flips + 1
    return {"enabled": enabled, "n": n, "roi": None if roi is None else round(roi, 5),
            "flips": flips}


def reason(state: dict) -> str | None:
    """The pass reason for a disabled series, or None if it may trade.

    Formatted like the other veto reasons in the ledger (`skill_blocked ratio=1.83`) so
    that a `reasons` scan over the decisions table groups them without special-casing.
    """
    if state.get("enabled", True):
        return None
    return (f"series_disabled roi={state.get('roi')} over n={state.get('n')}"
            f" (<= {OFF_ROI}; needs > {ON_ROI} to re-enable)")


def veto(state: dict) -> str | None:
    """The ACTED-ON answer: `reason(state)` in live mode, always None under `SHADOW`.

    The single point where the switch is read. `reason()` stays the pure statement of what
    the gate thinks — it is what gets recorded, displayed and tested — and this is the only
    thing any caller may branch on. Both lanes call it (`ops/decide_all` and
    `research/pit_gates.GateState.disabled`), which is what makes SHADOW one switch instead
    of two that can disagree.
    """
    return None if SHADOW else reason(state)


def state(conn, series: str) -> dict:
    """The stored §25.4 verdict for `series`, or `{}` if it has never been evaluated.

    Split out of `blocked` for #155: the shadow record needs the whole state (roi, n) and
    the veto needs only the reason, and loading twice would let the recorded verdict and
    the acted-on one come from different rows if the weekly job landed between them.
    """
    import json
    r = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='series_enable' AND series=?"
        " ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()
    if not r:
        return {}
    try:
        # `or "{}"`: metrics_json is nullable, and json.loads(None) raises TypeError,
        # which a bare `except ValueError` does NOT catch — it would propagate out of
        # decide_all's per-series loop and abort the whole daily cycle. The siblings
        # (calibration._load, conformal.sizing_factor) already guard it this way.
        return json.loads(r["metrics_json"] or "{}") or {}
    except (ValueError, TypeError):
        return {}


def record_shadow(conn, series: str, st: dict, now) -> None:
    """#155. Write today's verdict to `shadow_series_enable`. **Never vetoes anything.**

    One row per (day, series), `INSERT OR REPLACE`, so the several `decide_all` cycles in a
    day are idempotent rather than 96 near-duplicates.

    Rows are written for ENABLED series too, and that is the point: without them "the gate
    never wanted to block anything" and "the recorder was dead" look identical, which is
    the failure mode `exits.shadow_run` calls out and `blocked()` below was actually bitten
    by. A series with no stored verdict at all is written as `evaluated=0` rather than
    skipped, for the same reason.
    """
    conn.execute(
        "INSERT OR REPLACE INTO shadow_series_enable(day, series, evaluated, would_block,"
        " roi, n, flips, reason, ts_utc) VALUES(?,?,?,?,?,?,?,?,?)",
        (now.date().isoformat(), series, int(bool(st)), int(reason(st) is not None),
         st.get("roi"), st.get("n"), st.get("flips"), reason(st), now.isoformat()))


def would_block(conn, series: str) -> str | None:
    """What the gate says, ignoring `SHADOW` — the observation, never the action.

    Callers that branch on this are asserting they want the counterfactual. Everything on
    the decision path must use `blocked`/`veto` instead.
    """
    return reason(state(conn, series))


def blocked(conn, series: str) -> str | None:
    """Live-path entry point: the reason this series may not trade today, or None.

    **Under `SHADOW` this returns None unconditionally** — see that constant. It still
    performs the read, so a stored verdict that cannot be parsed is still caught here
    rather than at the moment the switch is flipped.

    Reads the `series_enable` experiments row that `eval.run_series` writes, rather than
    re-running `decision_replay` — that call is minutes per series and `decide_all` runs
    over the whole registry every day. Absent row ⇒ None (trade), which is the correct
    default for a veto: a gate that has never been evaluated must not block.

    Refresh cadence is therefore WEEKLY (`ops/refresh.py` step `weekly_eval_gates` →
    `eval.run_all`), which is the same cadence the calibration map and the capture
    memory already refresh on. A series that turns bad mid-week keeps trading until the
    weekly run; that lag is accepted rather than paid for with a daily full replay.

    **The absent row must be LOUD.** Fail-open is right; failing open in silence is not.
    This module landed 2026-08-05, one day after the last `eval.run_all` pass, so on
    2026-08-06 there were ZERO `series_enable` rows in the live db and this returned None
    for all 14 series — while the published d75 backtest, which computes the same fold
    PIT-wise instead of reading the artefact, recorded `series_disabled: 13`. A gate the
    backtest applies and the live lane cannot is a divergence in the optimistic direction,
    and nothing anywhere said so. `unevaluated()` below is how a caller can tell "this
    series may trade" from "this gate has never run".
    """
    return veto(state(conn, series))


def unevaluated(conn, series_list) -> list[str]:
    """Which of `series_list` have no stored §25.4 verdict at all.

    Separate from `blocked` because they are different answers: `blocked` returning None
    means "may trade", and that must stay cheap and total. This says whether the answer
    was computed or merely defaulted.
    """
    have = {r["series"] for r in conn.execute(
        "SELECT DISTINCT series FROM experiments WHERE name='series_enable'")}
    return [s for s in series_list if s not in have]
