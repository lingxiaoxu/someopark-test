"""#153 — the board's "latest decision" must consider every OPEN kind.

`frontend_export` has two panels asking a version of "what is the latest decision on this
(series, period)?", and each carried its own hand-typed kind list:

    board   (macro_board.json)  ('open','pass','exit')
    stances (macro_bets.json)   ('open','argmax','arb','snipe','pass')

They are allowed to differ in ONE token. The board reports what last happened, so a close
belongs in it. The stances table reports the standing actionable stance, and `exit` must
stay out of it because the frontend renders every non-`pass` kind as a live bet with a green
stake (`MacroArtifact.tsx`: `const isBet = d && d.kind !== 'pass'`) — an exit listed there
would display a closed position as an open one.

What was wrong is the board's list dropping three of the four OPEN_KINDS. An argmax, arb or
snipe open could not be the board's latest decision, so the board would fall back to an older
`pass` and report "we passed" on a period the desk had actually opened.

Latent on today's book (argmax is 4 rows, none newest on its period), so this changes 0 of 66
live board rows — which is the reason to fix it now rather than after it costs something.
"""
from __future__ import annotations

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import frontend_export as fx
from prediction_market_macro.ops.ledger import OPEN_KINDS

S, P = "KXWTIW", "2026-08-07"
COLS = "id, kind"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _row(conn, kind: str, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES('2026-08-06T12:00:00+00:00',?,?,'{}',?,1.0,'{}','m/1.0','{}',?)",
        (S, P, kind, note))
    conn.commit()
    return cur.lastrowid


@pytest.mark.parametrize("kind", list(OPEN_KINDS))
def test_the_board_sees_every_open_kind_as_the_latest_decision(conn, kind):
    """The bug: with a `pass` underneath and an open of `kind` on top, the board used to
    report the pass for three of the four kinds."""
    _row(conn, "pass", "no_edge")
    newest = _row(conn, kind)
    got = fx._latest_decision(conn, S, P, COLS, include_close=True)
    assert got["id"] == newest and got["kind"] == kind


@pytest.mark.parametrize("kind", list(OPEN_KINDS))
def test_the_stances_table_sees_every_open_kind_too(conn, kind):
    """The stances list already had all four. Pinned so the shared helper cannot regress
    the panel that was correct while fixing the one that was not."""
    _row(conn, "pass", "no_edge")
    newest = _row(conn, kind)
    got = fx._latest_decision(conn, S, P, COLS, include_close=False)
    assert got["id"] == newest and got["kind"] == kind


def test_exit_is_the_one_token_the_two_panels_disagree_on(conn):
    """The live KXNATGASW 2026-08-07 shape: an `open`, then a `pass`, then an `exit`. The
    board must show the exit — the frontend would paint an exit in the stances table as a
    green live bet, which is why it is excluded there."""
    _row(conn, "open")
    _row(conn, "pass", "already_open_no_averaging_down")
    exited = _row(conn, "exit", "edge_reversal")

    assert fx._latest_decision(conn, S, P, COLS, include_close=True)["id"] == exited


@pytest.mark.parametrize("close_kind", ["exit", "settle_note", "cancel"])
def test_a_close_leaves_no_standing_stance(conn, close_kind):
    """#153b. Excluding `exit` from the stances set is not enough on its own: it leaves the
    panel showing the stance UNDERNEATH the close, and that stance is routinely false once
    the position is gone. The live row said `pass / already_open_no_averaging_down` on a
    position that had been exited — a customer-facing claim to be holding something closed.

    Parametrised over all of CLOSE_KINDS: a settle_note and a cancel end a position just as
    an exit does, and each has a live period sitting in exactly this state."""
    _row(conn, "open")
    _row(conn, "pass", "already_open_no_averaging_down")
    _row(conn, close_kind, "closed")
    assert fx._latest_decision(conn, S, P, COLS, include_close=False) is None


def test_the_stance_refills_on_the_next_decision(conn):
    """The blank must be transient, not sticky — the next `decide_all` tick writes a fresh
    row on that period and the panel must pick it up. If this failed, a period would go
    dark permanently after its first close."""
    _row(conn, "open")
    _row(conn, "pass", "already_open_no_averaging_down")
    _row(conn, "exit", "edge_reversal")
    assert fx._latest_decision(conn, S, P, COLS, include_close=False) is None
    fresh = _row(conn, "pass", "no_edge")
    assert fx._latest_decision(conn, S, P, COLS, include_close=False)["id"] == fresh


def test_a_close_on_another_period_does_not_blank_this_one(conn):
    """The supersede rule is per (series, period). Cheap to get wrong by dropping a WHERE
    clause, and wrong in the direction that empties the whole panel."""
    p = _row(conn, "pass", "no_edge")
    conn.execute("INSERT INTO decisions(ts_utc, series, period, structure_json, kind,"
                 " size_usd, inputs_json, model_version, gate_snapshot, note)"
                 " VALUES('2026-08-06T13:00:00+00:00',?,'2026-09','{}','exit',1.0,'{}',"
                 "'m/1.0','{}','')", (S,))
    conn.commit()
    assert fx._latest_decision(conn, S, P, COLS, include_close=False)["id"] == p


def test_the_two_kind_sets_differ_by_exactly_one_token(conn):
    """The invariant that keeps this from drifting back into two hand-typed lists: adding a
    fifth open kind must reach BOTH panels, and the only licensed difference is `exit`."""
    board = set(fx._stance_kinds(True))
    stances = set(fx._stance_kinds(False))
    assert board - stances == {"exit"}
    assert stances - board == set()
    assert set(OPEN_KINDS) <= stances, "every open kind must reach both panels"


def test_neither_panel_reports_a_settle_note_or_a_cancel_as_a_decision(conn):
    """Both sets deliberately stop short of the full CLOSE_KINDS — a `settle_note` or a
    `cancel` is never itself rendered as the decision. Pinned because widening the sets is
    a display change, not a bug fix, and should not happen by accident.

    The board therefore still falls back to the newest actionable row; the stances panel
    blanks instead, under #153b."""
    _row(conn, "open")
    p = _row(conn, "pass", "no_edge")
    _row(conn, "settle_note")
    _row(conn, "cancel")
    board = fx._latest_decision(conn, S, P, COLS, include_close=True)
    assert board["id"] == p and board["kind"] == "pass"
    assert fx._latest_decision(conn, S, P, COLS, include_close=False) is None
