"""research/live_replay.py — roll the backtest forward to yesterday and reconcile it
against what the live book actually did over the same days.

WHY THIS EXISTS
---------------
The displayed track has two segments joined at `TRACK_CUTOVER` (2026-08-11): a frozen
backtest before it, the production ledger after it. The backtest itself is not frozen —
`weekly_walkforward_30d` re-runs an unbounded 30-day window — but nothing ever ran the
walk-forward over *exactly the live window* and put the two side by side. So the one
comparison that can catch a silent divergence between the researched strategy and the
shipped one was the comparison nobody was making.

WHAT IT DOES **NOT** CLAIM
--------------------------
It does not assert the two paths must agree. They cannot, and `walkforward.run`'s own
docstring says so in four places. The replay:

  * takes at most ONE trade per event — `opened` is keyed by event and never cleared,
    so an exit-then-re-enter that live is free to do is invisible here (wf.py ~#142 note);
  * decides once a day at the candle close (`offset_hour`, default 16:00Z) while live
    decides every refresh cycle (~09:16Z) and every event-window tick;
  * runs with `gates["min_leg_depth_usd"] = 0.0`, because candles carry no depth —
    strictly MORE permissive than live;
  * has NO entry-side circuit breaker, because `breaker_tripped` reads unacked alerts and
    `alerts` stores no ack timestamp, so there is no PIT way to know at simulated day D
    whether an alert had been acked (wf.py, the long comment above `blocked_by`);
  * can only see events that have SETTLED with real candle rows — a position still open
    today is structurally absent from the replay.

Every one of those biases the replay toward taking trades live did not. So the artifact
here is not an equality check but a **divergence accounting**: each difference is charged
to one of those known causes, and anything left over lands in `UNEXPLAINED`, which is the
only bucket worth waking up for. An equality check would fire every single day and mean
nothing; this fires when the shipped rule has drifted from the researched one.

THE SECOND HALF: WHAT WE LEFT ON THE TABLE
------------------------------------------
`opportunity()` reads every `kind='pass'` decision in the window and splits the reasons
into what the STRATEGY declined (no edge, too early, Kelly under a contract) versus what
the INFRASTRUCTURE ate (stale quotes, a tripped breaker). The split matters because only
one of the two is a research question. On the first live window it read 32.3% infra —
a third of the window was not a strategy decision at all.

The counterfactual attached to the infra bucket is deliberately weak-form: it reports
what the REPLAY did on the same (series, period, day) that live blocked. That is a
simulation at a daily candle close with no depth model and an n in the single digits.
It is a pointer at where to look, not a claim about foregone money, and `run()` labels it
that way in the payload rather than trusting the reader to remember.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops.frontend_export import TRACK_CUTOVER

# Reason -> (bucket, is_structural_replay_divergence).
#
# `structural` means: the replay CANNOT reproduce this gate, by documented design, so a
# replay-only trade carrying this reason is explained and must not raise an alarm.
# `infra` means: the strategy never got to decide. That is an operations finding.
# `strategy` means: the rule looked and declined — the two paths genuinely disagreed
# about price or parameters, which for a once-a-day candle close vs an every-cycle live
# read is expected but is worth counting, because a big number there means the daily
# granularity is no longer a rounding error.
_REASONS = {
    "circuit_breaker":                ("infra",     True),   # no breaker in the harness
    "stale_inputs":                   ("infra",     True),   # harness reads candles, never stale
    "depth_fail":                     ("market",    True),   # min_leg_depth_usd = 0.0 here
    "already_open_no_averaging_down": ("book",      True),   # `opened` never cleared
    "skill_blocked":                  ("research",  False),  # db_gates DO model this
    "series_disabled":                ("research",  False),
    "too_far_from_close":             ("strategy",  False),
    "too_close_to_close":             ("strategy",  False),
    "entropy_gate":                   ("strategy",  False),
    "penny_leg":                      ("strategy",  False),
    "kelly_below_one_contract":       ("strategy",  False),
    "no_edge":                        ("strategy",  False),
}
# Which reason wins when live cited several for one key on one day. Ordered by how far up
# the pipeline the gate sits: a breaker that aborts before the model runs explains the
# divergence more completely than an edge test that ran and declined.
_PRIORITY = ("circuit_breaker", "stale_inputs", "skill_blocked", "series_disabled",
             "depth_fail", "already_open_no_averaging_down", "entropy_gate",
             "penny_leg", "kelly_below_one_contract", "too_close_to_close",
             "too_far_from_close", "no_edge")


def _reason(note: str | None) -> str:
    """First token of a pass note. `decide_all` writes `stale_inputs pred=27h quotes=8.1h`
    and `already_open_no_averaging_down`, so the token before any space/=/:/( is the gate.
    Unknown tokens are returned verbatim and fall through to `other` — inventing a bucket
    for them would hide a gate someone added without telling this file."""
    n = (note or "").strip()
    for sep in (" ", "=", ":", "("):
        n = n.split(sep)[0]
    return n or "unknown"


def _window(end: datetime | None) -> tuple[datetime, datetime, int]:
    """[cutover, end] and the `days` that spans, for walkforward's window arithmetic.

    Default end is the last instant of YESTERDAY, not `now`: a replay that includes today
    would be comparing a full simulated day against a live day that is still running, and
    every event still open at this hour would read as a divergence that resolves itself
    overnight. Yesterday is the newest day both paths have finished.
    """
    cut = datetime.fromisoformat(TRACK_CUTOVER)
    if end is None:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0)
        end = today - timedelta(microseconds=1)          # 23:59:59.999999 yesterday
    days = max(1, (end.date() - cut.date()).days)
    return cut, end, days


# ─────────────────────────── the two paths ───────────────────────────

def replay(conn, end: datetime | None = None, log=None) -> dict:
    """Walk-forward over the live window, stored under its own `hash_tag`.

    The tag is not cosmetic. `walkforward.run` writes `INSERT OR REPLACE` keyed on
    (name, config_hash), and config_hash is built from `d{days}:{fair}:end{date}`. A
    live-window run is a different strategy question from the canonical 30/60-day
    headline rows that `frontend_export` reads with `window LIKE '30d%'`; without the tag
    a window that happened to be 30 days long would silently overwrite the headline.
    """
    from prediction_market_macro.research import walkforward
    _, sim_end, days = _window(end)
    return walkforward.run(conn, days=days, end=sim_end, hash_tag="livewin",
                           select_mode="argmin", log=log)


def live_entries(conn, since: str, until: str) -> list[dict]:
    """Every position the live book OPENED in the window, with its closure if it has one.

    Same source and same kind filter `frontend_export` uses for `track.live`, so this
    cannot drift from the displayed record. Pairing comes from `ops.ledger.closures`,
    which pairs a close to the position it closed rather than to the period — the
    distinction that #150 was about.
    """
    from prediction_market_macro.ops import ledger as _ledger
    closed_by = {d["id"]: d["close"] for d in _ledger.closures(conn)}
    out = []
    for r in conn.execute(
            "SELECT * FROM decisions WHERE kind IN ('open','argmax','arb','snipe')"
            " AND ts_utc>=? AND ts_utc<=? ORDER BY id", (since, until)).fetchall():
        st = json.loads(r["structure_json"] or "{}")
        close = closed_by.get(r["id"])
        fee = conn.execute("SELECT COALESCE(SUM(fee_usd),0) f FROM fills"
                           " WHERE decision_id=?", (r["id"],)).fetchone()["f"]
        realized = _ledger.realized_usd(close) if close else None
        out.append({"id": r["id"], "day": r["ts_utc"][:10], "series": r["series"],
                    "period": r["period"], "kind": r["kind"], "desc": st.get("desc"),
                    "staked": round((r["size_usd"] or 0) + fee, 4),
                    "realized": realized,
                    "closed_by": close["kind"] if close else None,
                    "open": close is None})
    return out


def pass_index(conn, since: str, until: str) -> dict:
    """(series, period, day) -> Counter(reason), plus the same keyed without the day.

    Both views are needed. The day-level one is the precise comparison — "on the day the
    replay bought, what did live say about that exact event?" The window-level one is the
    fallback for when live never evaluated the key that day at all, which is itself a
    finding (the series was not in any scheduled lane, or the refresh died before
    reaching it) rather than a missing explanation.
    """
    by_day: dict[tuple[str, str, str], Counter] = defaultdict(Counter)
    by_key: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in conn.execute(
            "SELECT series, period, ts_utc, note FROM decisions WHERE kind='pass'"
            " AND ts_utc>=? AND ts_utc<=?", (since, until)).fetchall():
        reason = _reason(r["note"])
        by_day[(r["series"], r["period"], r["ts_utc"][:10])][reason] += 1
        by_key[(r["series"], r["period"])][reason] += 1
    return {"by_day": by_day, "by_key": by_key}


# ─────────────────────────── the accounting ───────────────────────────

def _explain_replay_only(t: dict, idx: dict) -> tuple[str, str]:
    """Charge one replay-only trade to a cause. Returns (bucket, detail).

    Reads the day first, the window second, and refuses to guess: if live never wrote a
    decision for that key in the whole window, the answer is UNEXPLAINED and says so.
    """
    key = (t["series"], t["period"])
    day = t["day"]
    day_r = idx["by_day"].get((*key, day))
    if day_r:
        for r in _PRIORITY:
            if day_r[r]:
                bucket, structural = _REASONS.get(r, ("other", False))
                return (("STRUCTURAL:" + r) if structural else ("DISAGREED:" + r),
                        f"live cited {r} x{day_r[r]} on {day}")
        top = day_r.most_common(1)[0]
        return "UNEXPLAINED", f"live cited unknown gate {top[0]!r} x{top[1]} on {day}"
    win_r = idx["by_key"].get(key)
    if win_r:
        # Live evaluated this event, just not on the day the replay bought it. That is the
        # granularity divergence in its purest form: the replay looks once a day at a
        # candle close, live looks every cycle, and the two picked different days to care.
        return ("STRUCTURAL:different_day",
                f"live evaluated this event on other days only ({dict(win_r)})")
    return "UNEXPLAINED", "live never wrote any decision for this event in the window"


def _event_facts(conn, series: str, key: str) -> tuple[int, datetime | None]:
    """(real candle rows, event close) for the event the model calls `key`.

    `contracts.period` holds Kalshi's own token (`26AUG1414`, `26JUL`); every trade and
    decision record in this file holds the model's period KEY (`2026-08-14`, `2026-07`).
    They are never equal — 0 of 8872 contract rows have an ISO-shaped period — so the
    `c.period = ?` filter this function replaces matched nothing on EVERY event, always
    returned 0, and turned `no_candles` into an alibi that fitted any live-only trade.
    `research/pit_gates.period_closes` carries the same warning and does the same join;
    this is that join, done the same way.

    `cd.end_ts > 0` matters: kalshi_md writes a NULL-price sentinel row at end_ts=0 when
    the candlestick endpoint 404s, and 6700 of 14683 candle rows are that sentinel.
    Counting them would read "priceable" exactly when it is not.
    """
    from prediction_market_macro.util.periods import kalshi_period_to_key
    n, close = 0, None
    for r in conn.execute(
            "SELECT c.period p, MAX(c.close_time) ct,"
            " SUM(CASE WHEN cd.end_ts>0 AND cd.yes_ask_close IS NOT NULL THEN 1 ELSE 0 END) n"
            " FROM contracts c LEFT JOIN candles cd ON cd.ticker=c.ticker"
            " WHERE c.series=? GROUP BY c.period", (series,)):
        if kalshi_period_to_key(r["p"]) != key:
            continue
        n += r["n"] or 0
        if r["ct"]:
            ct = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
            close = ct if close is None else max(close, ct)
    return n, close


def _explain_live_only(t: dict, replay_keys: Counter, conn, feat: dict,
                       sim_end: datetime) -> tuple[str, str]:
    """Charge one live-only trade to a cause.

    Ordered most-structural first, and deliberately ending somewhere reachable. The
    previous version could only ever return one of three answers, two of which required a
    re-entry or an open position, so every remaining live-only trade fell into
    `no_candles` — including events with hundreds of candle rows. That made `UNEXPLAINED`
    unreachable on this side of the ledger and the ALIGNED verdict vacuous: it was not
    that nothing was unexplained, it was that nothing COULD be.

    The two ends of the list are the ones worth reading. `outside_window` is not a defect
    at all — the replay stops at yesterday, so a position live opened on a still-unsettled
    event is invisible to it and will fold in on its own once the event closes.
    `UNEXPLAINED` now means the replay built a placeable bet on this very event and yet
    the event is missing from its traded set, which no documented limit explains.
    """
    key = (t["series"], t["period"])
    if replay_keys.get(key):
        return ("STRUCTURAL:no_reentry",
                "replay's `opened` dict is keyed by event and never cleared")
    if t["open"]:
        return ("STRUCTURAL:still_open",
                "replay only walks SETTLED events; this position has not closed")
    n, close = _event_facts(conn, t["series"], t["period"])
    if close is not None and close > sim_end:
        return ("STRUCTURAL:outside_window",
                f"event closes {close.date()}, after the replay's cut-off"
                f" {sim_end.date()} — it settles into a later window, not this one")
    if n == 0:
        return ("STRUCTURAL:no_candles",
                "no real candle rows for this event — the replay cannot price it")
    rows = feat.get(key) or []
    blocked = [r["blocked_by"] for r in rows if r.get("blocked_by")]
    if blocked:
        # The replay priced the same event and its OWN gate refused. Live's gates are the
        # shipped ones, so this is a genuine disagreement about a gate rather than about a
        # price — the mirror of `_explain_replay_only`'s DISAGREED bucket.
        top = Counter(blocked).most_common(1)[0]
        return (f"DISAGREED:replay_{top[0]}",
                f"replay priced it and its own {top[0]} gate blocked it x{top[1]}")
    if rows:
        return ("UNEXPLAINED",
                f"replay built {len(rows)} placeable bet(s) on this event"
                f" ({n} candle rows) yet never traded it")
    return ("DISAGREED:replay_declined",
            f"event was priceable ({n} candle rows) and settled inside the window, but no"
            " leg cleared the replay's edge rule at any daily candle close")


def reconcile(conn, wf: dict, live: list[dict], since: str, until: str) -> dict:
    """Divergence accounting between the replay's hybrid stream and the live book.

    The HYBRID stream is the right comparand, not `edge`: `decide_all` calls
    `_place_argmax` only when the edge decision passed, so live holds one leg per event
    chosen by exactly that rule. Comparing against `edge` alone would count every live
    favourite bet as an unexplained divergence.
    """
    idx = pass_index(conn, since, until)
    # Every event the replay PRICED, traded or not. `feature_rows` carries the blocked
    # ones too (walkforward #147: placed=False / blocked_by=<gate>), which is the only
    # way to tell "the replay's gate refused" apart from "the replay saw no edge".
    feat: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for fr in wf.get("feature_rows") or []:
        feat[(fr["series"], fr["period"])].append(fr)
    sim_end = datetime.fromisoformat(until)
    rep = [{"series": t["series"], "period": t["period"],
            "day": t["day"], "desc": t["desc"], "staked": t["staked"],
            "realized": t["realized"], "won": t["won"]}
           for t in wf["streams"]["hybrid"]["trades"]]
    rep_keys = Counter((t["series"], t["period"]) for t in rep)
    live_keys = Counter((t["series"], t["period"]) for t in live)

    matched, replay_only, live_only = [], [], []
    for t in rep:
        k = (t["series"], t["period"])
        if live_keys.get(k):
            matched.append({**t, "n_live_on_key": live_keys[k]})
        else:
            bucket, detail = _explain_replay_only(t, idx)
            replay_only.append({**t, "bucket": bucket, "detail": detail})
    seen: Counter = Counter()
    for t in live:
        k = (t["series"], t["period"])
        seen[k] += 1
        # The FIRST live entry on a key the replay also traded is the match; the 2nd+ are
        # the re-entries the replay cannot represent, so they belong in live_only.
        if rep_keys.get(k) and seen[k] == 1:
            continue
        bucket, detail = _explain_live_only(t, rep_keys, conn, feat, sim_end)
        live_only.append({**t, "bucket": bucket, "detail": detail})

    def _tally(rows):
        c: Counter = Counter()
        for r in rows:
            c[r["bucket"]] += 1
        return dict(c.most_common())

    unexplained = ([r for r in replay_only if r["bucket"] == "UNEXPLAINED"]
                   + [r for r in live_only if r["bucket"] == "UNEXPLAINED"])
    return {
        "n_replay": len(rep), "n_live": len(live), "n_matched": len(matched),
        "n_replay_only": len(replay_only), "n_live_only": len(live_only),
        "replay_only_by_cause": _tally(replay_only),
        "live_only_by_cause": _tally(live_only),
        "n_unexplained": len(unexplained),
        "unexplained": unexplained,
        "matched": matched,
        "replay_only": replay_only,
        "live_only": live_only,
        "verdict": ("ALIGNED — every difference charged to a documented harness limit"
                    if not unexplained else
                    f"REVIEW — {len(unexplained)} difference(s) no known limit explains"),
        "note": "the replay is strictly MORE permissive than live (no breaker, no depth"
                " gate, no staleness) and strictly LESS able to re-enter, so"
                " replay_only >> live_only is the expected shape, not a defect",
    }


def opportunity(conn, since: str, until: str, wf: dict | None = None) -> dict:
    """What the window declined, split by who declined it — the rule, or the plumbing.

    Every `kind='pass'` decision carries a gate token; this counts them, per day and in
    total, and separates `infra` (the strategy never ran) from `strategy` (it ran and
    said no). The first is an ops backlog, the second is a research question, and
    reporting them as one number — "we passed 2107 times" — answers neither.
    """
    rows = conn.execute(
        "SELECT series, period, ts_utc, note FROM decisions WHERE kind='pass'"
        " AND ts_utc>=? AND ts_utc<=?", (since, until)).fetchall()
    by_reason: Counter = Counter()
    by_bucket: Counter = Counter()
    per_day: dict[str, Counter] = defaultdict(Counter)
    infra_keys: set[tuple[str, str, str]] = set()
    for r in rows:
        reason = _reason(r["note"])
        bucket = _REASONS.get(reason, ("other", False))[0]
        day = r["ts_utc"][:10]
        by_reason[reason] += 1
        by_bucket[bucket] += 1
        per_day[day][bucket] += 1
        if bucket == "infra":
            infra_keys.add((r["series"], r["period"], day))
    n = len(rows)
    days_out = {}
    for d in sorted(per_day):
        tot = sum(per_day[d].values())
        days_out[d] = {"n_pass": tot, "infra": per_day[d]["infra"],
                       "infra_share": round(per_day[d]["infra"] / tot, 4) if tot else None,
                       "by_bucket": dict(per_day[d].most_common())}

    # Weak-form counterfactual: on the exact (series, period, day) live lost to infra,
    # what did the replay do? Anything here is a SIMULATED trade at a daily candle close
    # with zero depth — a pointer, not a P&L claim, and the caller must print the caveat.
    cf = {"n_infra_blocked_events": len(infra_keys), "n_replay_traded": 0,
          "replay_realized": 0.0, "replay_staked": 0.0, "trades": []}
    if wf:
        for t in wf["streams"]["hybrid"]["trades"]:
            if (t["series"], t["period"], t["day"]) in infra_keys:
                cf["n_replay_traded"] += 1
                cf["replay_realized"] = round(cf["replay_realized"] + t["realized"], 4)
                cf["replay_staked"] = round(cf["replay_staked"] + t["staked"], 4)
                cf["trades"].append({"series": t["series"], "period": t["period"],
                                     "day": t["day"], "desc": t["desc"],
                                     "staked": t["staked"], "realized": t["realized"]})
    cf["caveat"] = ("simulated at a daily candle close with min_leg_depth_usd=0 and no"
                    " breaker; n is tiny; read as WHERE TO LOOK, never as foregone P&L")
    return {"n_pass": n, "by_reason": dict(by_reason.most_common()),
            "by_bucket": dict(by_bucket.most_common()),
            "infra_share": round(by_bucket["infra"] / n, 4) if n else None,
            "per_day": days_out, "counterfactual": cf}


# ─────────────────────────── entry point ───────────────────────────

def run(conn, end: datetime | None = None, store: bool = True, log=print) -> dict:
    """Roll the replay to `end` (default: yesterday), reconcile, attribute, store.

    Stored under `experiments(name='live_replay')` keyed on the window end, so the series
    of daily runs is addressable and a later question — "when did this first diverge?" —
    is a SELECT rather than a re-run of a 20-minute simulation.
    """
    cut, sim_end, days = _window(end)
    since, until = cut.isoformat(), sim_end.isoformat()
    if log:
        log(f"[live_replay] window {cut.date()} .. {sim_end.date()}  ({days}d)")
    wf = replay(conn, end=sim_end, log=None)
    live = live_entries(conn, since, until)
    rec = reconcile(conn, wf, live, since, until)
    opp = opportunity(conn, since, until, wf)
    out = {"window_start": cut.date().isoformat(),
           "window_end": sim_end.date().isoformat(), "days": days,
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "replay": {"n_trades": wf["streams"]["hybrid"]["n_trades"],
                      "won": wf["streams"]["hybrid"]["won"],
                      "staked": wf["streams"]["hybrid"]["staked"],
                      "realized": wf["streams"]["hybrid"]["realized"],
                      "roi": wf["streams"]["hybrid"]["roi"]},
           "live": {"n_trades": len(live),
                    "n_open": sum(1 for t in live if t["open"]),
                    "won": sum(1 for t in live if t["realized"] is not None
                               and t["realized"] > 0),
                    "staked": round(sum(t["staked"] or 0 for t in live), 4),
                    "realized": round(sum(t["realized"] for t in live
                                          if t["realized"] is not None), 4)},
           "reconciliation": rec, "opportunity": opp}
    if store:
        conn.execute(
            "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
            " metrics_json, created_ts) VALUES('live_replay',?,'*',?,?,?)",
            (f"livewin:end{sim_end.date().isoformat()}", f"{days}d:live",
             json.dumps(out, ensure_ascii=False), out["generated_at"]))
        # An unexplained divergence means the shipped rule and the researched rule have
        # parted company in a way this file does not know about. That is exactly the class
        # of thing that goes unnoticed for weeks, so it raises rather than only logging.
        if rec["n_unexplained"]:
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (out["generated_at"], "warn", "research.live_replay",
                 f"{rec['n_unexplained']} unexplained replay/live divergence(s) over"
                 f" {cut.date()}..{sim_end.date()}: "
                 + "; ".join(f"{u['series']}/{u['period']} {u['detail']}"
                             for u in rec["unexplained"][:5])))
        conn.commit()
    if log:
        log(f"[live_replay] replay {out['replay']['n_trades']} trades"
            f" / live {out['live']['n_trades']} — {rec['verdict']}")
        log(f"[live_replay] pass reasons: {opp['by_bucket']}"
            f"  infra_share={opp['infra_share']}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--end", help="last simulated day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--no-store", action="store_true",
                    help="do not write the experiments row or any alert")
    ap.add_argument("--no-lock", action="store_true",
                    help="skip the single-instance lock (for a manual second look)")
    ap.add_argument("--json", action="store_true", help="dump the full payload")
    a = ap.parse_args()
    end = None
    if a.end:
        end = (datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc)
               + timedelta(days=1) - timedelta(microseconds=1))
    s = load_settings(require_keys=False)
    # `refuse, don't queue` — same reasoning as ops/refresh: a second simulation of the
    # same window would burn ten minutes to INSERT OR REPLACE the identical row, and two
    # of them writing at once is how 2026-08-18 produced `database is locked`.
    from prediction_market_macro.ops.refresh import RefreshBusy, _single_instance
    from contextlib import nullcontext
    lock = (nullcontext() if a.no_lock
            else _single_instance(s.output_dir, "live_replay.lock"))
    try:
        with lock:
            conn = init_db(s.db_path)
            out = run(conn, end=end, store=not a.no_store)
    except RefreshBusy as e:
        print(f"[live_replay] another replay is already running ({e}) — skipping")
        return
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({k: v for k, v in out.items()
                          if k not in ("reconciliation", "opportunity")},
                         ensure_ascii=False, indent=2))
        r, o = out["reconciliation"], out["opportunity"]
        print(f"\nreconciliation: matched={r['n_matched']}"
              f" replay_only={r['n_replay_only']} live_only={r['n_live_only']}"
              f" unexplained={r['n_unexplained']}")
        print(f"  replay_only: {r['replay_only_by_cause']}")
        print(f"  live_only:   {r['live_only_by_cause']}")
        print(f"  {r['verdict']}")
        print(f"\nopportunity: n_pass={o['n_pass']} infra_share={o['infra_share']}")
        print(f"  by_reason: {o['by_reason']}")


if __name__ == "__main__":
    main()
