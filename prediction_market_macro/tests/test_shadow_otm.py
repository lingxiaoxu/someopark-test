"""PR-13 (#186) — the forward scorer for the deep-OTM cumulative-ladder sell.

Three things here are load-bearing, and they are the three that a later edit would most
plausibly get wrong in the flattering direction:

1. **The universe.** PR-13's discovery table was computed under "ladder scope" meaning a
   CUMULATIVE ladder — a survival curve of `print > K_i` legs. `REGISTRY.structure` uses
   the same word for `KXWTIW`, whose legs are `between`/`less` brackets, and swapping one
   for the other moves the discovery mean from +0.0526 to +0.0253 on the same window.
   `event_structure` is the ex-ante rule that keeps the forward window measuring the
   registered thing, and the tests below pin it against the discovery script's own
   settlement-based `classify()` — including the fact that the two select the identical
   64 events / 156 legs.

2. **Execution at the BID.** Selection is on the mid; the fill is the bid. A mid-to-mid
   version of this is an accounting identity that pays no spread and would turn a +0.05
   edge into a much larger one for free.

3. **The window is forward.** `run` must never score an event that closed before the
   registration instant, because the band and the direction were both chosen after
   looking at the discovery sample.

Everything else in the module is a readout. Readouts get one test each; these three get
the adversarial ones.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.research import shadow_otm as O

REG = datetime.fromisoformat(O.REGISTERED)
SEEN = "2021-01-01T00:00:00+00:00"      # first_seen_ts is NOT NULL; nothing here reads it


def _legs(*families):
    return [{"ticker": f"T{i}", "strike_type": f, "result": "no"}
            for i, f in enumerate(families)]


# ── the universe: event_structure ─────────────────────────────────────────────
def test_an_all_greater_event_is_a_cumulative_ladder():
    assert O.event_structure(_legs(*["greater"] * 40)) == "cumulative_ladder"


def test_greater_or_equal_is_the_same_one_sided_family_as_greater():
    """KXJOBLESSCLAIMS lists `greater_or_equal` and every other ladder lists `greater`.
    They are the same shape — leg i settles YES iff the print clears K_i, so the YES legs
    nest. Omitting `greater_or_equal` from ONE_SIDED silently drops all 13 KXJOBLESSCLAIMS
    events of the discovery sample, which is 46 of its 156 legs."""
    assert O.event_structure(_legs(*["greater_or_equal"] * 15)) == "cumulative_ladder"
    assert "greater_or_equal" in O.ONE_SIDED


def test_a_bracket_event_is_a_partition_even_though_the_registry_calls_it_a_ladder():
    """KXWTIW's actual shape: 13 `between` brackets plus one `less` and one `greater`
    tail. Exactly one leg can settle YES, so every leg is cheap by construction and the
    in-band YES rate is pinned by the partition rather than by any longshot bias."""
    ev = _legs(*(["between"] * 13 + ["less", "greater"]))
    assert O.event_structure(ev) == "partition"

    from prediction_market_macro.config.registry import REGISTRY
    assert REGISTRY["KXWTIW"].structure == "ladder"      # ...and the registry disagrees
    assert "KXWTIW" in O.ladder_series()                 # so the prefilter lets it in
    # which is exactly why the prefilter is not the decision.


def test_a_missing_strike_type_is_unknown_and_not_quietly_a_partition():
    """604 backfill-era contracts carry a NULL. Calling them `partition` excludes them
    correctly today but would make a future ingest regression indistinguishable from an
    ordinary bracket market — i.e. it would prune the forward sample silently."""
    assert O.event_structure(_legs("greater", None, "greater")) == "unknown"
    assert O.event_structure(_legs("")) == "unknown"
    assert O.event_structure([]) == "unknown"


def test_event_structure_reads_no_settlement_so_it_can_run_at_entry_time():
    """The discovery script's `classify()` needed `sum(l["out"])`. A universe rule that
    reads the settlement cannot define a FORWARD window at all — at T−1h there is no
    outcome to read. This asserts the replacement is blind to it."""
    yes = [dict(l, result="yes") for l in _legs(*["greater"] * 5)]
    no = [dict(l, result="no") for l in _legs(*["greater"] * 5)]
    assert O.event_structure(yes) == O.event_structure(no) == "cumulative_ladder"
    # and it does not even need the key to be present
    assert O.event_structure([{"strike_type": "greater"}]) == "cumulative_ladder"


def test_the_family_rule_and_the_discovery_heuristic_pick_the_same_universe():
    """The identity that makes the swap legitimate rather than a re-scope.

    `classify()` verbatim from `/tmp/dfm_verify/otm_sell.py`, the script the registered
    table came out of. On the 81 discovery events the two rules agree 81/81. Reproduced
    here on the shapes that actually occur, since the cached dump is not in the repo:
    all-`greater` ladders of every YES count, `greater_or_equal` ladders, and the KXWTIW
    bracket. The agreement is not a coincidence — a bracket partition settles exactly one
    YES and its mids sum to about 1, which is what `classify` keys on.
    """
    def classify(legs):                                  # otm_sell.py:66
        k = sum(l["out"] for l in legs)
        return "partition" if (k == 1.0 and len(legs) > 2
                               and sum(l["mid"] for l in legs) < 1.5) else "ladder"

    cases = [
        # (families, per-leg (mid, out)) — a cumulative ladder with 21/40 YES
        (["greater"] * 40, [(0.9, 1.0)] * 21 + [(0.05, 0.0)] * 19),
        # a claims ladder, 3 of 15 YES
        (["greater_or_equal"] * 15, [(0.8, 1.0)] * 3 + [(0.06, 0.0)] * 12),
        # the KXWTIW bracket: exactly one YES, mids summing to ~1.05
        (["between"] * 13 + ["less", "greater"],
         [(0.07, 0.0)] * 13 + [(0.10, 1.0), (0.04, 0.0)]),
    ]
    for fams, book in cases:
        legs = [{"strike_type": f, "mid": m, "out": o}
                for f, (m, o) in zip(fams, book)]
        mine = O.event_structure(legs) == "cumulative_ladder"
        theirs = classify(legs) == "ladder"
        assert mine is theirs, (fams[:2], mine, theirs)


def test_ladder_series_is_a_prefilter_that_only_removes_impossible_series():
    """It must never exclude a series that `event_structure` would accept, or the cheap
    prefilter would be doing the deciding."""
    from prediction_market_macro.config.registry import REGISTRY
    keep = set(O.ladder_series())
    assert "KXFEDDECISION" not in keep and REGISTRY["KXFEDDECISION"].structure != "ladder"
    for s in ("KXNATGASW", "KXAAAGASW", "KXJOBLESSCLAIMS", "KXCPI", "KXGDP"):
        assert s in keep


# ── execution: the bid, and the fee ───────────────────────────────────────────
def test_pnl_is_the_bid_minus_the_outcome_minus_the_entry_fee():
    from prediction_market_macro.strategy.edge import taker_fee
    for bid in (0.02, 0.07, 0.19, 0.34):
        for out in (0.0, 1.0):
            assert O.leg_pnl(bid, out) == pytest.approx(bid - out - taker_fee(bid, 1))


def test_selling_yes_at_the_bid_is_buying_no_at_one_minus_it():
    """`taker_fee` is symmetric in p <-> 1-p, so the NO-space accounting — risk `1 - b`,
    payoff 1 on a NO settlement — is the same number. If this ever fails, `leg_pnl`'s
    one-line form has stopped being the trade it claims to be."""
    from prediction_market_macro.strategy.edge import taker_fee
    for bid in (0.02, 0.11, 0.28):
        for out in (0.0, 1.0):
            no_space = (1.0 - out) - (1.0 - bid) - taker_fee(1.0 - bid, 1)
            assert O.leg_pnl(bid, out) == pytest.approx(no_space)


def test_the_fee_is_charged_once_at_entry_and_never_at_settlement():
    """A loser and a winner at the same bid differ by exactly 1.00, not by 1.00 plus a
    second fee. Kalshi charges nothing at settlement."""
    assert O.leg_pnl(0.20, 0.0) - O.leg_pnl(0.20, 1.0) == pytest.approx(1.0)


def test_a_cheap_leg_can_lose_money_even_when_it_settles_no():
    """The penny-lottery floor. At a 1c bid the 1c fee eats the entire premium, which is
    why the band starts at 0.02 and why widening it would manufacture significance."""
    assert O.leg_pnl(0.01, 0.0) <= 0.0


# ── the harness ───────────────────────────────────────────────────────────────
def _db(tmp_path, events):
    """A db with just enough of `candles`/`contracts`/`settlements` for `_events` and
    `_market_leg_bar`. events: [(series, period, close, [(strike_type, result, bid, ask)])]
    """
    from prediction_market_macro.ingest.store import init_db
    conn = init_db(str(tmp_path / "t.db"))
    for series, period, close, legs in events:
        for i, (st, res, bid, ask) in enumerate(legs):
            tk = f"{series}-{period}-{i}"
            conn.execute(
                "INSERT INTO contracts(ticker,series,event_ticker,period,sub_title,"
                "strike_type,floor_strike,cap_strike,close_time,status,first_seen_ts)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (tk, series, f"{series}-{period}", period, tk, st, float(i), None,
                 close.isoformat().replace("+00:00", "Z"), "settled", SEEN))
            conn.execute(
                "INSERT INTO settlements(ticker,series,period,result,settled_ts,"
                "first_seen_ts) VALUES(?,?,?,?,?,?)",
                (tk, series, period, res, close.isoformat(), SEEN))
            conn.execute(
                "INSERT INTO candles(ticker,end_ts,yes_bid_close,yes_ask_close,volume)"
                " VALUES(?,?,?,?,?)",
                (tk, int((close - timedelta(hours=2)).timestamp()), bid, ask, 10))
    conn.commit()
    return conn


def _ladder(bid, ask, result="no", n=1):
    return [("greater", result, bid, ask)] * n


def test_score_takes_the_bid_not_the_mid(tmp_path, monkeypatch):
    """The single most reversible choice in the construction. A 0.10/0.20 quote has a mid
    of 0.15 (in band) and a bid of 0.10; the leg must be booked at 0.10."""
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    close = REG + timedelta(days=3)
    conn = _db(tmp_path, [("KXNATGASW", "26SEP01", close, _ladder(0.10, 0.20))])
    sc = O.score(conn, asof=close + timedelta(days=1))
    assert sc["n_legs"] == 1
    leg = sc["legs"][0]
    assert leg["mid"] == pytest.approx(0.15) and leg["bid"] == pytest.approx(0.10)
    assert leg["pnl"] == pytest.approx(O.leg_pnl(0.10, 0.0))
    assert leg["pnl"] < O.leg_pnl(0.15, 0.0)          # the spread is paid, not booked


def test_score_selects_on_the_mid_and_the_band_is_half_open(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    close = REG + timedelta(days=3)
    conn = _db(tmp_path, [("KXNATGASW", "26SEP01", close, [
        ("greater", "no", 0.01, 0.03),      # mid 0.02 -> IN, the lower edge is closed
        ("greater", "no", 0.34, 0.36),      # mid 0.35 -> OUT, the upper edge is open
        ("greater", "no", 0.00, 0.03),      # mid 0.015 -> below the band
        ("greater", "no", 0.30, 0.32),      # mid 0.31 -> in
    ])])
    sc = O.score(conn, asof=close + timedelta(days=1))
    assert sorted(round(l["mid"], 3) for l in sc["legs"]) == [0.02, 0.31]
    assert sc["drops"]["out_of_band"] == 2


def test_score_refuses_a_leg_nobody_bids_for(tmp_path, monkeypatch):
    """Mid 0.10 from a 0.00/0.20 book. The sale is not executable at any size, and
    booking it at the mid would record the whole spread as profit."""
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    close = REG + timedelta(days=3)
    conn = _db(tmp_path, [("KXNATGASW", "26SEP01", close, _ladder(0.00, 0.20))])
    sc = O.score(conn, asof=close + timedelta(days=1))
    assert sc["n_legs"] == 0 and sc["drops"]["no_bid"] == 1


def test_score_excludes_a_bracket_event_and_says_so(tmp_path, monkeypatch):
    """The KXWTIW case end to end: in-band, quoted, settled — and still not scored,
    with the reason in the output rather than absorbed into a drop counter."""
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXWTIW",))
    close = REG + timedelta(days=3)
    legs = [("between", "no", 0.08, 0.12)] * 13 + [("less", "yes", 0.08, 0.12),
                                                   ("greater", "no", 0.08, 0.12)]
    conn = _db(tmp_path, [("KXWTIW", "26SEP0114", close, legs)])
    sc = O.score(conn, asof=close + timedelta(days=1))
    assert sc["n_legs"] == 0 and sc["drops"]["not_cumulative"] == 15
    assert len(sc["structure_disagreement"]) == 1
    d = sc["structure_disagreement"][0]
    assert d["series"] == "KXWTIW" and d["event_structure"] == "partition"
    assert d["registry_structure"] == "ladder"
    assert d["families"] == ["between", "greater", "less"]


def test_a_null_strike_type_in_the_forward_window_raises_a_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    close = REG + timedelta(days=3)
    conn = _db(tmp_path, [("KXNATGASW", "26SEP01", close,
                           [("greater", "no", 0.08, 0.12), (None, "no", 0.08, 0.12)])])
    out = O.run(conn, asof=close + timedelta(days=1), model_filter_readout=False)
    assert out["drops"]["unknown_structure"] == 1
    assert out["data_warning"] and "ingest regression" in out["data_warning"]
    assert out["n_forward"] == 0


def test_a_clean_forward_window_carries_no_structure_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    close = REG + timedelta(days=3)
    conn = _db(tmp_path, [("KXNATGASW", "26SEP01", close, _ladder(0.08, 0.12, n=3))])
    out = O.run(conn, asof=close + timedelta(days=1), model_filter_readout=False)
    assert out["data_warning"] is None and out["n_forward"] == 3


# ── the window is forward ─────────────────────────────────────────────────────
def test_run_never_scores_an_event_that_closed_before_the_registration(tmp_path,
                                                                      monkeypatch):
    """The band and the direction were chosen after looking at the discovery sample, so
    the only clean evidence this hypothesis will ever have is what settles after
    `REGISTERED`. A combined number would be a flattering one."""
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    before, after = REG - timedelta(days=10), REG + timedelta(days=10)
    conn = _db(tmp_path, [
        ("KXNATGASW", "26AUG18", before, _ladder(0.08, 0.12, n=5)),
        ("KXNATGASW", "26SEP08", after, _ladder(0.08, 0.12, n=2))])
    out = O.run(conn, asof=after + timedelta(days=1), model_filter_readout=False)
    assert out["n_forward"] == 2 and out["n_events"] == 1
    # ...and the discovery window is reachable ONLY through the explicit override
    assert O.score(conn, asof=after + timedelta(days=1),
                   since=before.isoformat())["n_legs"] == 7


def test_run_does_not_score_an_event_that_has_not_closed_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "ladder_series", lambda: ("KXNATGASW",))
    close = REG + timedelta(days=30)
    conn = _db(tmp_path, [("KXNATGASW", "26SEP28", close, _ladder(0.08, 0.12, n=4))])
    assert O.run(conn, asof=REG + timedelta(days=5),
                 model_filter_readout=False)["n_forward"] == 0


# ── the verdict ───────────────────────────────────────────────────────────────
def _out(n, pnl, p=0.0001):
    return {"n_forward": n, "n_required": O.N_FORWARD, "wilcoxon_p_one_sided": p,
            "total_pnl": pnl, "mean_pnl_per_leg": pnl / max(n, 1)}


def test_verdict_is_pending_until_the_registered_leg_count_exists():
    assert O._verdict(_out(149, 40.0)).startswith("PENDING")
    assert "149/150" in O._verdict(_out(149, 40.0))


def test_a_positive_total_alone_does_not_pass_it():
    assert O._verdict(_out(150, 3.0, p=0.02)).startswith("FALSIFIED")


def test_significance_alone_does_not_pass_it_either():
    """Both halves of the registration are required: the Wilcoxon AND a positive total.
    A significant NEGATIVE median must not be read as a pass."""
    assert O._verdict(_out(150, -3.0, p=1e-9)).startswith("FALSIFIED")


def test_the_bar_is_bonferroni_not_nominal():
    """K=26 was itemised at registration time. Scoring at 0.05 would pass a p of 0.01
    that the registration explicitly does not."""
    assert O._verdict(_out(150, 5.0, p=0.01)).startswith("FALSIFIED")
    assert O._verdict(_out(150, 5.0, p=0.0018)).startswith("PASSED")
    assert O.ALPHA_BONFERRONI == pytest.approx(0.05 / 26)


def test_a_degenerate_sample_is_inconclusive_rather_than_falsified():
    o = _out(150, 0.0, p=None)
    assert O._verdict(o).startswith("INCONCLUSIVE")


def test_a_pass_authorises_nothing_at_the_router():
    assert "separate forward registration" in O._verdict(_out(150, 9.0, p=1e-6))


# ── the registration constants ────────────────────────────────────────────────
def test_the_registered_constants_are_the_ones_in_the_registration():
    assert O.BAND == (0.02, 0.35) and O.N_FORWARD == 150 and O.K_DISCOVERY == 26
    assert O.OFFSET == "-1h" and O.REGISTERED.startswith("2026-08-28")
    assert O.DISCOVERY["universe"] == "cumulative_ladder"
    assert (O.DISCOVERY["n_events"], O.DISCOVERY["n_legs"]) == (64, 156)


def test_the_fingerprint_tracks_the_fee_schedule_not_a_model():
    """PR-13 reads no model, so the thing that can silently change the trade is
    `taker_fee`. A fee change makes every forward leg a different bet."""
    import hashlib
    import pathlib
    p = (pathlib.Path(O.__file__).resolve().parent.parent / "strategy" / "edge.py")
    assert O.code_fingerprint() == hashlib.sha1(p.read_bytes()).hexdigest()[:12]
    assert "taker_fee" in p.read_text()


def test_an_unstamped_registration_says_so_rather_than_claiming_no_change():
    note = O.code_change_note("deadbeef")
    if O.REGISTERED_FINGERPRINT == "PENDING":
        assert note["code_changed_since_registration"] is None
    else:
        assert note["code_changed_since_registration"] is True
        assert "UNDOCUMENTED CHANGE" in note["note"]


# ── the cluster bootstrap is a readout, and must resample events ──────────────
def test_the_bootstrap_resamples_events_so_one_wide_ladder_cannot_carry_it():
    """41 of the discovery sample's 156 legs came from KXAAAGASW alone. Resampling legs
    would treat them as 41 independent draws; they share one print."""
    legs = ([{"series": "A", "period": "1", "pnl": 0.5}] * 40
            + [{"series": "B", "period": str(i), "pnl": -0.1} for i in range(10)])
    ci = O._cluster_ci(legs, reps=800)
    assert ci["n_events"] == 11                       # not 50
    assert ci["ci95"][0] < 0.0 < ci["ci95"][1]        # the one big event can be dropped


def test_the_bootstrap_refuses_a_sample_too_small_to_resample():
    assert O._cluster_ci([{"series": "A", "period": "1", "pnl": 0.1}] * 3) is None


def test_wilcoxon_drops_zeros_and_refuses_a_powerless_sample():
    assert O._wilcoxon([0.0] * 40) == (None, None)
    assert O._wilcoxon([0.1, 0.2, 0.3, 0.4]) == (None, None)
    stat, p = O._wilcoxon([0.1] * 20)
    assert p is not None and p < 0.05


def test_by_series_breaks_out_the_series_that_carried_the_discovery_margin():
    legs = [{"series": "KXAAAGASW", "period": "1", "pnl": 0.5, "outcome": 0.0},
            {"series": "KXNATGASW", "period": "1", "pnl": -0.5, "outcome": 1.0}]
    bs = O._by_series(legs)
    assert bs["KXAAAGASW"]["mean"] == pytest.approx(0.5)
    assert bs["KXNATGASW"]["n_yes"] == 1
