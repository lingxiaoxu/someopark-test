"""#149 — a close must retire the position it actually closed, not "one on this period".

Four functions answer "do I already hold this (series, period)?", and three of them used
to count ONE kind of open against EVERY kind of close:

    ledger.has_open              kind='open'                    vs exit|cancel|settle_note
    arb._has_open_arb            kind='arb'                     vs exit|cancel|settle_note
    snipe._has_open_snipe        kind='snipe'                   vs exit|cancel|settle_note
    decide_all._has_any_open     open|argmax|arb|snipe          vs exit|cancel|settle_note

Only the last is balanced. The first three decrement on closes they never incremented, so
enough foreign-stream closes and the check reads False while the position is live —
whereupon `decide()` sees `already_open=False` and opens a second one, because that flag is
the only thing between it and averaging down (strategy/decision.py:65).

It is not hypothetical. KXWTIW 2026-08-07 held 2 edge opens + 4 argmax + 3 argmax exits, so
`has_open` computed 2 > 3 = False and decisions 3638 / 3697 became a real duplicate
position 15 seconds apart — same ticker, same price, two decision rows, two fill sets.

The fix is attribution: closes now carry `closes_decision_id`, and all four checks read one
`ledger.open_decisions`. Rows written before that column existed have it NULL and are
retired FIFO, which is what the old `open_positions` query already gave them — replaying
the live ledger both ways agreed on all 8 open positions and all 66 periods.
"""
from __future__ import annotations

import json

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import ledger

TS = "2026-08-06T18:00:00+00:00"
S, P = "KXWTIW", "2026-08-07"


@pytest.fixture()
def conn(tmp_path):
    return init_db(str(tmp_path / "t.db"))


def _open(conn, kind, ticker="T-B78.50", series=S, period=P):
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note)"
        " VALUES(?,?,?,?,?,1.0,'{}','m/1.0','{}','')",
        (TS, series, period,
         json.dumps({"kind": "single", "desc": f"YES {ticker}",
                     "legs": [{"ticker": ticker, "side": "yes", "price": 0.5}]}), kind))
    conn.commit()
    return cur.lastrowid


def _close(conn, kind="exit", closes=None, series=S, period=P):
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc, series, period, structure_json, kind, size_usd,"
        " inputs_json, model_version, gate_snapshot, note, closes_decision_id)"
        " VALUES(?,?,?,'{}',?,1.0,'{}','m/1.0','{}','',?)",
        (TS, series, period, kind, closes))
    conn.commit()
    return cur.lastrowid


# ── the bug itself ────────────────────────────────────────────────────────────────────

def test_closing_an_argmax_leg_does_not_retire_an_edge_position(conn):
    """The live KXWTIW shape, minimised. Under the old SQL `has_open` returned 2 > 3 =
    False here and the book opened a third position on a ticker it already held twice."""
    a1, a2, a3 = (_open(conn, "argmax") for _ in range(3))
    e1 = _open(conn, "open")
    e2 = _open(conn, "open")
    for a in (a1, a2, a3):
        _close(conn, "exit", closes=a)

    assert ledger.has_open(conn, S, P) is True
    kinds = [d["kind"] for d in ledger.open_decisions(conn, S, P)]
    assert kinds == ["open", "open"], kinds
    assert {d["id"] for d in ledger.open_decisions(conn, S, P)} == {e1, e2}


def test_each_stream_sees_only_its_own_positions(conn):
    """All four checks, one ledger. `_has_any_open` is the balanced one and was already
    right; it is here so a change that fixes three by breaking the fourth fails loudly."""
    from prediction_market_macro.ops.decide_all import _has_any_open
    from prediction_market_macro.strategy.arb import _has_open_arb
    from prediction_market_macro.strategy.snipe import _has_open_snipe

    arb_id = _open(conn, "arb")
    _open(conn, "snipe")
    assert (ledger.has_open(conn, S, P), _has_open_arb(conn, S, P),
            _has_open_snipe(conn, S, P), _has_any_open(conn, S, P)) == \
        (False, True, True, True)

    _close(conn, "exit", closes=arb_id)          # closes the ARB leg only
    assert (ledger.has_open(conn, S, P), _has_open_arb(conn, S, P),
            _has_open_snipe(conn, S, P), _has_any_open(conn, S, P)) == \
        (False, False, True, True)


def test_a_close_on_another_period_is_never_visible(conn):
    """Scoping is by (series, period) as well as by position — a close on next week's
    contract must not free this week's slot."""
    _open(conn, "open")
    other = _open(conn, "open", period="2026-08-14")
    _close(conn, "exit", closes=other, period="2026-08-14")
    assert ledger.has_open(conn, S, P) is True


# ── the legacy bridge ─────────────────────────────────────────────────────────────────

def test_unattributed_legacy_closes_still_retire_fifo(conn):
    """40 of the 53 exits on the live book predate the column. If they retired nothing,
    every historical position would read as still open and `exits`/`settle` would start
    re-processing a book that closed weeks ago — a far worse failure than the one being
    fixed. FIFO is not a guess: it is the reading the old query already gave them."""
    first = _open(conn, "open")
    second = _open(conn, "open")
    _close(conn, "exit", closes=None)                       # legacy row

    live = ledger.open_decisions(conn, S, P)
    assert [d["id"] for d in live] == [second], "oldest-first is the legacy reading"
    assert first not in [d["id"] for d in live]


def test_attributed_and_legacy_closes_coexist(conn):
    """The live ledger is exactly this: old NULL rows, then new attributed ones."""
    old1 = _open(conn, "open")
    _close(conn, "exit", closes=None)                       # retires old1 by FIFO
    keep = _open(conn, "open")
    doomed = _open(conn, "argmax")
    _close(conn, "exit", closes=doomed)                     # retires doomed by id

    assert [d["id"] for d in ledger.open_decisions(conn, S, P)] == [keep]
    assert old1 not in [d["id"] for d in ledger.open_decisions(conn, S, P)]


def test_an_attributed_close_is_not_also_charged_to_someone_else(conn):
    """The two branches are exclusive. If an attributed close ALSO fell through to the
    FIFO pop it would retire two positions per row, and the symptom — a position quietly
    vanishing from `open_positions`, so exits stops managing it and settle never books
    it — is silent."""
    a = _open(conn, "open")
    b = _open(conn, "open")
    _close(conn, "exit", closes=b)
    assert [d["id"] for d in ledger.open_decisions(conn, S, P)] == [a]


def test_a_close_naming_an_unknown_id_retires_nothing(conn):
    """Defensive: an id that is not open (already closed, or another period's) must be a
    no-op, not a silent fallback to FIFO. Falling back would make a double-close retire a
    healthy position."""
    a = _open(conn, "open")
    _close(conn, "exit", closes=a)
    _close(conn, "exit", closes=a)                          # double close of the same id
    b = _open(conn, "open")
    assert [d["id"] for d in ledger.open_decisions(conn, S, P)] == [b]


# ── writers and schema ────────────────────────────────────────────────────────────────

def test_every_close_writer_records_what_it_closed():
    """Three writers emit close kinds. A fourth added without the column would reopen the
    bug on that path only, which is the hardest version to notice."""
    import inspect

    from prediction_market_macro.ops import exits, pnl, retire_stale_book
    for mod in (exits, pnl, retire_stale_book):
        src = inspect.getsource(mod)
        assert "closes_decision_id" in src, mod.__name__


def test_the_migration_is_idempotent_on_a_db_that_predates_the_column(tmp_path):
    """`decisions` is CREATE TABLE IF NOT EXISTS, so the DDL alone never reaches the live
    ledger — only the ALTER does, and it has to survive being run on every startup."""
    path = str(tmp_path / "legacy.db")
    conn = init_db(path)
    conn.execute("ALTER TABLE decisions RENAME TO decisions_new")
    conn.execute("CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ts_utc TEXT NOT NULL, series TEXT NOT NULL, period TEXT NOT NULL,"
                 " structure_json TEXT NOT NULL, kind TEXT NOT NULL, fair REAL, ask REAL,"
                 " net_edge REAL, size_usd REAL, inputs_json TEXT NOT NULL,"
                 " model_version TEXT NOT NULL, gate_snapshot TEXT NOT NULL, note TEXT)")
    conn.commit()
    conn.close()

    for _ in range(2):                                      # twice: it must be idempotent
        conn = init_db(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        assert "closes_decision_id" in cols
        conn.close()


def test_open_positions_still_carries_fills(conn):
    """`ops/exits` iterates `open_positions` and prices each leg off `pos["fills"]`. The
    rewrite changed how the rows are selected; dropping the fills would make every
    position unexitable while every check still reported it held."""
    did = _open(conn, "open")
    conn.execute("INSERT INTO fills(decision_id, ts_utc, ticker, side, price, count,"
                 " fee_usd, mode) VALUES(?,?, 'T-B78.50', 'yes', 0.87, 1, 0.01, 'paper')",
                 (did, TS))
    conn.commit()
    pos = ledger.open_positions(conn)
    assert len(pos) == 1 and len(pos[0]["fills"]) == 1
    assert pos[0]["id"] == did and pos[0]["fills"][0]["price"] == 0.87


def test_no_check_keeps_a_private_copy_of_the_counting_rule():
    """#141's lesson, applied before it costs anything: the four checks disagreed for as
    long as they were four implementations. This fails if one is re-inlined."""
    import inspect

    from prediction_market_macro.ops import decide_all
    from prediction_market_macro.strategy import arb, snipe
    for fn in (ledger.has_open, arb._has_open_arb, snipe._has_open_snipe,
               decide_all._has_any_open):
        src = inspect.getsource(fn)
        assert "open_decisions" in src, fn.__qualname__
        assert "SUM(CASE" not in src, f"{fn.__qualname__} re-inlined the old SQL"
