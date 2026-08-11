"""#147 — `shadow_blocked` must make the gated region READABLE without moving any PnL.

A §25.3 feature row is written only for a bet that was PLACED, so a gate-aborted event
leaves no trace at all. For `ser_roi` that is not merely lossy, it is disqualifying: §25.4
disables a series exactly when `ser_roi <= 0`, so the column is observed on 4 of 70 rows
and every observed value is POSITIVE. The feature is truncated at the very sign boundary
its own premise is about (§25.17), and no coefficient fitted on it can test that premise in
either direction. `skill_ratio` (9/70) is truncated the same way by `skill_blocked`.

`shadow_blocked` lets those events run all the way through and emits a `placed=False` row
for them. Everything here exists to pin the ONE property that makes it safe:

    it changes no PnL, in either state.

Most of these are source-text assertions rather than behavioural ones, and that is
deliberate. Every guard below is an omission-failure: delete it and nothing raises, no
existing test moves, and the only symptom is a backtest that quietly disagrees with the one
that produced the displayed number. That is the same failure shape as #132's `staked` bug,
which survived into a published figure. The end-to-end proof is the paired A/B in
PLAN_EXTENSION §25.19 — bit-identical `trades`/`curve`/`roi` with the flag on and off —
which is far too slow to live in this suite.
"""
from __future__ import annotations

import inspect

from prediction_market_macro.research import walkforward


def _src() -> str:
    return inspect.getsource(walkforward)


def _run_body() -> str:
    s = _src()
    return s[s.index("def run("):s.index("def coverage(")]


def test_the_flag_is_off_by_default():
    """Research flags default OFF. A diagnostic that turns itself on is not a diagnostic —
    it is an undocumented change of strategy."""
    sig = inspect.signature(walkforward.run)
    assert sig.parameters["shadow_blocked"].default is False


def test_blocked_events_are_kept_out_of_pending_scores():
    """THE guard. `pending_scores` flushes into `pool_runner`, which sets the
    `fair_mode='pooled'` log-pool weights — so crediting a blocked event's Brier scores
    would move the pooled pmf, the structs and finally the PnL. Production `continue`s
    before the model runs on a gated series, so those scores were never available to the
    live path either.

    Without this line `shadow_blocked` is PnL-neutral only when `fair_mode='model'`, and
    silently NOT neutral on the pooled runs — which are the ones used for display.
    """
    body = _run_body()
    seg = body[body.index("pending_scores.append") - 700:
               body.index("pending_scores.append")]
    assert "blocked_by is None" in seg


def test_a_blocked_row_is_never_booked_as_a_trade():
    """`trades` is built from `opened.values()`, so the feature row is harmless but the
    BOOKING is not: `opened` feeds trades/curve/roi, and `open_rows`/`opened_today` feed
    `_sim_risk_veto`, which would then reject real trades on later days because of capital
    consumed by a bet that never happened."""
    body = _run_body()
    for anchor in ('opened[(ev["series"], key)] = row',
                   'opened_argmax[(ev["series"], key)] = row_a'):
        seg = body[body.index(anchor) - 400:body.index(anchor)]
        assert "if blocked_by is None:" in seg, anchor


def _enclosing_blocks(body: str, pos: int) -> list[str]:
    """The `if`/`for`/`with` headers that lexically contain `body[pos]`, innermost first.

    Indentation-based, which is exact for Python and is the point: the earlier version of
    the guard test scanned back a fixed 500 characters, so a comment or one extra level of
    nesting between a guard and the line it guards read as UNGUARDED. That fired as a false
    failure on the F6 wallet fix while the guard was fully intact — a test that cries wolf
    on correct code gets weakened by whoever hits it next, and this one is protecting a
    silent-corruption property (#147) that nothing else can catch. Containment is what the
    assertion actually means, so assert containment.
    """
    lines = body[:pos].splitlines()
    if not lines:
        return []
    indent = len(lines[-1]) - len(lines[-1].lstrip())
    out = []
    for line in reversed(lines[:-1]):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        this = len(line) - len(line.lstrip())
        if this < indent:
            out.append(line.strip())
            indent = this
            if this == 0:
                break
    return out


def test_pnl_bearing_state_is_only_touched_under_the_guard():
    """The four accumulators that can move a headline. Pinned as a set so that ADDING a
    fifth one outside the guard is what fails, rather than being noticed later."""
    body = _run_body()
    for name in ("open_rows.append(", "opened_today +=", "day_trades +="):
        pos = body.find(name)
        while pos >= 0:
            # startswith, not equality: a guard line may carry a trailing `# ...` comment
            assert any(b.startswith("if blocked_by is None:")
                       for b in _enclosing_blocks(body, pos)), \
                f"{name} at {pos} is not under the #147 guard"
            pos = body.find(name, pos + 1)


def test_a_blocked_event_is_logged_once_not_once_per_day():
    """A blocked event never enters `opened`, so it is re-examined every simulated day
    until close. Without the dedup it emits ~7 near-duplicate rows per event, which both
    swamps the 70 placed rows and breaks the independence the LOEO fold assumes."""
    body = _run_body()
    assert "blocked_seen" in body
    seg = body[body.index("blocked_seen.add") - 500:body.index("blocked_seen.add")]
    assert "in blocked_seen" in seg


def test_the_feature_row_says_whether_the_bet_actually_happened():
    """A counterfactual row and a real one must not be indistinguishable downstream."""
    body = _run_body()
    row = body[body.index("feature_rows.append("):]
    row = row[:row.index("peak = ")]
    assert '"placed": blocked_by is None' in row
    assert '"blocked_by": blocked_by' in row


def test_the_stake_on_a_blocked_row_is_the_would_be_stake_not_zero():
    """The label has to stay on ONE scale with the placed rows or the two cannot be pooled
    at all. `placed=False` is what marks a row counterfactual — zeroing `staked` instead
    would make `realized/staked` undefined exactly on the rows this flag exists to add."""
    body = _run_body()
    row = body[body.index("feature_rows.append("):]
    row = row[:row.index("peak = ")]
    # 2026-08-11: stake now also carries entry taker fees (user display
    # convention, ROI floor -100%). Still fill_cost-based — the §25.2a
    # leg-sum inflation this test guards against remains impossible.
    assert '"staked": round(st.fill_cost(count) * count + entry_fees, 4)' in row


def test_a_shadow_run_cannot_overwrite_the_production_experiment_row():
    """Its PnL is identical BY CONSTRUCTION, which is exactly why a key collision here
    would be invisible: the headline would still read correctly while the stored
    `feature_rows` had silently changed shape underneath it."""
    body = _run_body()
    assert "':shadowblocked' if shadow_blocked else ''" in body
    # and both experiment rows must share one key, or the headline and its own feature
    # rows would be addressable under different hashes
    assert body.count("cfg_hash") >= 3


def test_the_two_gates_it_covers_are_the_two_that_truncate_the_columns():
    """`series_disabled` truncates `ser_roi`; `skill_blocked` truncates `skill_ratio`.
    Both are `continue`s that fire BEFORE the model runs, which is why neither leaves a
    row today."""
    body = _run_body()
    assert 'blocked_by = "skill_blocked"' in body
    assert 'blocked_by = "series_disabled"' in body
