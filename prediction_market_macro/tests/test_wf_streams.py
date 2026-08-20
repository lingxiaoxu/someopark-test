"""#157 — the six-stream harness, and the one property that makes it safe.

`research/walkforward.run` now carries every leg production has: `edge`, `argmax`,
`hybrid` (the live rule), plus the counterfactual `ml`/`blend` lines and the `arb`/
`snipe` censuses. The point of moving `ml` INTO this loop is that it then answers to the
same gates, exits and risk veto as `argmax`, so the two ROIs can finally be read side by
side — `research/selector.py`'s own loop has none of them, and its +104.9% vs argmax's
+17.5% was mostly a measurement of which harness was more permissive.

That is only worth anything if adding the streams did not MOVE the streams they are
compared against. These tests pin exactly that, by source inspection rather than by a
full run: a walk-forward over the real db takes ~7 minutes and needs candle history that
does not exist in a fixture, so an end-to-end assertion here would be either useless or
untestable. What CAN be pinned mechanically is the set of writes each stream is allowed
to make, and every way the isolation could rot is a write.

The empirical half of this was done once, by hand, on 2026-08-20: a 30-day run before
and after the change returned edge 13/+142.683%, argmax 7/+18.467%, hybrid 16/+121.782%
in both, with `gate_stats` identical except for the new `ml_risk_vetoed` key. These
tests are what stops that from silently stopping being true.
"""
from __future__ import annotations

import inspect
import re

from prediction_market_macro.research import walkforward


def _run_src() -> str:
    return inspect.getsource(walkforward.run)


def _trade_row_src() -> str:
    """`_trade_row` is a closure defined inside `run` — slice it out by indentation."""
    src = _run_src()
    start = src.index("def _trade_row(")
    rest = src[start:]
    # ends at the next line indented no further than `def _trade_row`'s own indent
    indent = len(src[:start].split("\n")[-1])
    out = []
    for i, line in enumerate(rest.split("\n")):
        if i and line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        out.append(line)
    return "\n".join(out)


# ── the isolation property ───────────────────────────────────────────────────

def test_shadow_rows_touch_none_of_the_three_shared_artefacts():
    """`_trade_row` has exactly three side effects, and a shadow call must make none.

    Named individually rather than as "no writes", because they fail differently:
      feature_rows  — trains research/confidence.py on a strategy nobody runs
      held_paths    — keyed `series/period/desc` with NO stream component, so an ml pick
                      that lands on the same struct as the edge pick OVERWRITES the edge
                      stream's published §25.5 path. Silent, and it corrupts a number
                      that is already displayed.
      book.stats    — the exit counters are how `gate_stats` reports the live lane;
                      a shadow exit inflating them makes the live number wrong
    """
    body = _trade_row_src()
    assert "_fr_sink = [] if shadow else feature_rows" in body
    assert re.search(r"if path and blocked_by is None and not shadow:", body)
    assert re.search(r"if not shadow:\n\s+book\.stats\[f\"exit_", body)


def test_the_shared_wallet_is_the_hybrid_book_alone():
    """`_open_rows` is what `_sim_risk_veto` charges against, so it is the single point
    where a counterfactual line could displace a real trade. It must still be built from
    `opened` + `opened_argmax` and nothing else — an `opened_ml` in here would let the
    shadow stream veto a hybrid bet, which is the one way these streams stop being free."""
    src = _run_src()
    start = src.index("def _open_rows(")
    # to the `return rows` that closes it — NOT to the next top-level statement, which
    # would swallow the counterfactual streams' own declarations and pass vacuously
    body = src[start:src.index("return rows", start)]
    assert "hybrid_book = list(opened.values())" in body
    assert "opened_ml" not in body and "opened_blend" not in body


def test_the_ml_line_is_vetoed_against_its_own_book():
    """Not against the hybrid book. Vetoing a counterfactual on somebody else's exposure
    would make its trade count a function of what the live rule happened to be holding —
    the one thing a counterfactual has to hold constant."""
    src = _run_src()
    assert "_ml_rows = _rows_of(opened_ml.values(), day)" in src
    assert "_sim_risk_veto(_ml_rows, ev[\"series\"], key" in src
    # ...and its vetoes are counted separately, so `risk_vetoed` stays a statement
    # about the live lane
    assert '"ml_risk_vetoed"' in inspect.getsource(walkforward._GateBook.__init__)


def test_blend_copies_a_row_it_does_not_rebuild_one():
    """A second `_trade_row` call would recompute `_mtm_path` for no new information and,
    when the underlying line is hybrid, write a SECOND non-shadow feature row for one
    bet — doubling that bet's weight in the §25.3 training set."""
    src = _run_src()
    blk = src[src.index("stream 5: BLEND"):src.index("streams 6 & 7")]
    assert "_br = dict(pick)" in blk
    assert "_trade_row(" not in blk


def test_the_ml_blind_spot_is_counted_not_hidden():
    """The event loop retires an event once BOTH live streams have entered, which the ml
    line never gets to see past. Closing it would make the loop run extra days and move
    `book.stats`, `param_select` and `pending_scores` — and `pending_scores` sets the
    pooled-mode log-pool weights, so it would move the hybrid PnL itself. A bounded blind
    spot beats a shadow stream that changes the thing it is measuring, but only if the
    bound is reported."""
    src = _run_src()
    assert 'cen["ml_event_days_skipped"] += 1' in src


# ── the censuses ─────────────────────────────────────────────────────────────

def test_arb_census_separates_unpriceable_from_rejected():
    """"4 violations, best net None" is unreadable: it cannot distinguish "the fee gate
    held" from "the structure was never priceable", and those are opposite conclusions
    about whether the money was ever real. Same three drop paths `arb.execute` has."""
    src = inspect.getsource(walkforward._arb_scan)
    assert src.count("dropped += 1") == 3
    assert '"dropped": dropped' in src


def test_the_arb_census_skips_categorical_books_the_way_the_live_leg_does():
    """`decide_all` guards its devig+arb block with `spec.structure != "categorical"`.
    A categorical book is a partition with no ordering, so `ladder_implied` reading it as
    a survival curve invents monotonicity violations out of a question that has no
    monotonicity. Counting those would have the census report opportunities the live
    stream is structurally never shown — i.e. it would answer the question it exists to
    answer with the opposite of the truth."""
    src = _run_src()
    # anchored on the section banner, not on `if census:` — and NOT ended on the bare
    # word SNIPE_SERIES, which first appears far earlier in the `cen` dict's own comment
    # and silently yields an empty slice that passes nothing
    blk = src[src.index("streams 6 & 7"):
              src.index("from prediction_market_macro.strategy.snipe import")]
    assert 'spec.structure != "categorical"' in blk
    assert blk.index('spec.structure != "categorical"') < blk.index("_arb_scan(")
    # ...and the event-day denominator moves with it, or the rate is computed against
    # a population the numerator was never allowed to draw from
    assert blk.index('spec.structure != "categorical"') < blk.index('cen["arb_event_days"]')


def test_arb_census_reports_no_dollars():
    """Archived candle rows carry `bid_depth`/`ask_depth` = 1e9, so 铁律 5's 20%-of-the-
    thinnest-leg cap cannot bind. A sized arb backtest would be a fiction about liquidity,
    and the honest shape for a depth-limited stream measured without depth is a count."""
    src = _run_src()
    blk = src[src.index('streams["arb"] ='):src.index('streams["snipe"] =')]
    assert '"n_trades": 0' in blk and '"realized": 0.0' in blk and '"roi": None' in blk
    assert "铁律 5" in blk


def test_snipe_census_is_counted_twice_on_purpose():
    """The wide count includes books the live sniper is not pointed at. If the two ever
    differ, the difference IS the finding — a post-print window on an unwatched series is
    a wiring gap, while a zero on the watched ones is the structural wall §24-B ran into.
    One aggregate number could not tell you which."""
    src = _run_src()
    assert "SNIPE_SERIES" in src
    assert 'cen["snipe_open_at_print_in_scope"]' in src
    assert 'cen["snipe_book_open_at_print"]' in src


# ── plumbing that must not drift ─────────────────────────────────────────────

def test_the_ml_feature_order_matches_the_selector_it_serves():
    """The column ORDER is the contract between `_MLBook.features` (which serves) and
    `selector._event_rows` (which trains). A column added on one side and not the other
    reassigns every coefficient silently — the model keeps predicting, just not the thing
    it was fitted for. Compared as source text because the two are deliberately separate
    copies: importing one into the other would couple this harness to selector's own
    loop, which is exactly the coupling #157 removed."""
    from prediction_market_macro.research import selector
    mine = inspect.getsource(walkforward._MLBook.features)
    theirs = inspect.getsource(selector._event_rows)
    for col in ("st.fair - st.cost", "abs(st.fair - st.cost)", "kind_yes, kind_no"):
        assert col in mine
    # the two share the family one-hot vocabulary rather than each spelling it out
    assert "FAMS" in mine and "FAMS" in theirs
    # ...and the same fallbacks, which are only visible on books with no spread/entropy
    assert "0.05 if spread_med is None" in mine and "spreads else 0.05" in theirs


def test_a_run_without_the_shadow_streams_gets_its_own_experiment_key():
    """Their PnL contribution is nil by construction, which is exactly why a collision
    would be invisible: `--no-shadow-streams` would overwrite the six-stream row with a
    three-stream one and the headline would still read correctly. Same failure shape as
    #147's `:shadowblocked`."""
    src = _run_src()
    assert "':noshadow'" in src
