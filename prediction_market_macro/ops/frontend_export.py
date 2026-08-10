"""ops/frontend_export.py — single writer of public/data/macro_*.json (PLAN §16.3).

The ONLY sanctioned write target outside the macro tree (0-bis whitelist (a)).
Small JSONs only; the frontend macro views (M7) read these at runtime via /data.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest import calendars as cal
from prediction_market_macro.util.periods import kalshi_period_to_key


def _sanitize(o):
    """Browsers' JSON.parse rejects bare NaN/Infinity (Python emits them by
    default) — every export goes through here: non-finite floats → null."""
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    return o


def _write(path, obj) -> None:
    path.write_text(json.dumps(_sanitize(obj), ensure_ascii=False, indent=1,
                               allow_nan=False))


# #153. Two panels on this page ask "what is the latest decision on this (series, period)?"
# and used two hand-written kind lists: the board took ('open','pass','exit') and the
# stances table took ('open','argmax','arb','snipe','pass'). They legitimately differ — the
# board reports what last HAPPENED, the stances table reports the standing ACTIONABLE
# stance, and an exit belongs in the first but not the second (the frontend renders any
# non-`pass` kind as a live bet with a green stake, so listing exits there would show closed
# positions as open ones). What was not legitimate is that the board's list omitted three of
# the four OPEN_KINDS: an argmax/arb/snipe open could not be the board's latest decision, so
# the board would have shown an older `pass` in its place.
#
# Latent today — argmax is 4 rows and none of them is the newest on its period, so widening
# the set changes 0 of 66 board rows. It is fixed now precisely because it costs nothing now.
# Both sets are derived from `ledger.OPEN_KINDS` rather than retyped, so a fifth open kind
# joins both automatically; the difference between them is one explicit token, `exit`.
def _stance_kinds(include_close: bool) -> tuple[str, ...]:
    from prediction_market_macro.ops.ledger import OPEN_KINDS
    return OPEN_KINDS + ("pass",) + (("exit",) if include_close else ())


def _latest_decision(conn, series: str, period: str, cols: str, *, include_close: bool):
    kinds = _stance_kinds(include_close)
    row = conn.execute(
        f"SELECT {cols} FROM decisions WHERE series=? AND period=? AND kind IN"
        f" ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT 1",
        (series, period, *kinds)).fetchone()
    if row is None or include_close:
        return row
    # #153b. A close SUPERSEDES the actionable row underneath it. Dropping `exit` from the
    # stances kind set stops an exit being painted as a live bet, but on its own it leaves
    # the panel showing whatever stance preceded the close — and that stance is routinely
    # false once the position is gone. Live right now: KXNATGASW 2026-08-07 was exited at
    # 18:31Z (id 3704) and the stances table reports the `pass` 5ms before it, whose stated
    # reason is `already_open_no_averaging_down` — a customer-facing claim that we are
    # holding a position we have closed.
    #
    # So: if the newest row of ANY kind is a close, there is no standing stance and the
    # panel says so (`decision: null` -> the frontend's own `—`). Nothing is hidden by
    # this — the board panel still carries the close, with its realized PnL. The state is
    # self-healing: the next `decide_all` tick writes a fresh pass or open on that period
    # and the row refills with something true.
    #
    # Four periods on the whole live ledger are in this state (KXWTIW 07-31, KXNATGASW
    # 07-31, KXAAAGASW 08-03, KXNATGASW 08-07); only the last is inside the stances panel's
    # -0.5..7.5 day window, so this changes exactly 1 of 9 displayed rows today.
    from prediction_market_macro.ops.ledger import CLOSE_KINDS
    allk = _stance_kinds(True) + CLOSE_KINDS
    newest = conn.execute(
        f"SELECT kind FROM decisions WHERE series=? AND period=? AND kind IN"
        f" ({','.join('?' * len(allk))}) ORDER BY id DESC LIMIT 1",
        (series, period, *allk)).fetchone()
    return None if newest and newest["kind"] in CLOSE_KINDS else row


def run(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    out_dir = settings.frontend_data
    written = []

    # ── macro_board.json: next releases + latest pred + decision per (series, period) ──
    board = {"generated_at": now.isoformat(), "next_releases": [], "series": {}}
    for spec in REGISTRY.values():
        nr = cal.next_release(spec.calendar, now)
        if nr:
            board["next_releases"].append({
                "series": spec.ticker, "family": spec.family, "cadence": spec.cadence,
                "period": nr.period, "scheduled_ts": nr.scheduled_ts.isoformat(),
                "note": nr.note})
        entries = []
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)).fetchall():
            key = kalshi_period_to_key(r["period"])
            if not key:
                continue
            # PRODUCTION model preds only — shadow members (chronos2/bridge/
            # ensemble) must never drive the board display
            pr = conn.execute(
                "SELECT asof, model_version, dist_json, ladder_json FROM preds"
                " WHERE series=? AND period=? AND model_version LIKE ?"
                " ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            de = _latest_decision(
                conn, spec.ticker, key,
                "ts_utc, kind, fair, ask, net_edge, size_usd, note, structure_json",
                include_close=True)          # #153: the board reports what last happened
            entries.append({
                "period": key,
                "pred": {"asof": pr["asof"], "model": pr["model_version"],
                         "dist": json.loads(pr["dist_json"])} if pr else None,
                "decision": dict(de) if de else None})
        board["series"][spec.ticker] = {"family": spec.family, "cadence": spec.cadence,
                                        "structure": spec.structure, "entries": entries}
    board["next_releases"].sort(key=lambda x: x["scheduled_ts"])
    _write(out_dir / "macro_board.json", board)
    written.append("macro_board.json")

    # ── macro_decisions.json: full ledger tail + open positions + marks ──
    dec = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions ORDER BY id DESC LIMIT 200").fetchall()]
    marks = [dict(r) for r in conn.execute(
        "SELECT * FROM marks WHERE ts=(SELECT MAX(ts) FROM marks)").fetchall()]
    _write(out_dir / "macro_decisions.json",
           {"generated_at": now.isoformat(), "decisions": dec, "latest_marks": marks})
    written.append("macro_decisions.json")

    # ── macro_coverage.json: lifecycle matrix + MISSED + alerts tail ──
    cov = [dict(r) for r in conn.execute(
        "SELECT * FROM coverage ORDER BY series, period").fetchall()]
    missed = [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE status='MISSED' ORDER BY due_ts DESC LIMIT 50").fetchall()]
    alerts = [dict(r) for r in conn.execute(
        "SELECT * FROM alerts WHERE acked=0 ORDER BY id DESC LIMIT 50").fetchall()]
    _write(out_dir / "macro_coverage.json",
           {"generated_at": now.isoformat(), "coverage": cov, "missed": missed,
            "alerts": alerts})
    written.append("macro_coverage.json")

    # ── macro_health.json: re-serialize through the sanitizer (never raw-copy) ──
    hp = settings.output_dir / "macro_health.json"
    if hp.exists():
        _write(out_dir / "macro_health.json", json.loads(hp.read_text()))
        written.append("macro_health.json")

    written.append(run_extended(conn, settings))
    return ",".join(written)


# Display cutover: the shown track is the frozen walk-forward BACKTEST before this instant
# and the actual production ledger from it on; positions opened earlier stay internal.
#
# Moved 2026-07-31T16:14 -> 2026-08-05T00:00 (#121). The old cutover never produced a live
# segment: 43 positions opened 7/28-7/31 under a rule set with no `max_days_to_close` held
# 50 (series, period) slots, so 842 of the 1090 decisions after it were
# `already_open_no_averaging_down` and nothing settled. Those are retired
# (ops/retire_stale_book), and the live record restarts here, clean, under current rules.
#
# The instant is midnight so it cannot overlap the backtest, which is bounded at
# `--end 2026-08-04`: an event enters the backtest only if it SETTLES by then. Overlap
# would count the same event once in each segment. See freeze_track's settle-span guard.
#
# The two segments are NOT the same kind of evidence and the UI must keep saying so —
# `macro.trackBacktest` / `macro.trackLive` chips plus `macro.trackNote`, which states in
# all five locales that the first segment was never traded. Do not merge or relabel them.
TRACK_CUTOVER = "2026-08-05T00:00:00+00:00"


def run_extended(conn, settings) -> str:
    """Additional exports: divergence / performance / oos / risk / fed detail."""
    now = datetime.now(timezone.utc)
    out_dir = settings.frontend_data
    written = []

    # ── macro_bets.json: the direct "what are we betting" view ──
    # ① open bets (placed, unsettled, with latest mark) ② today's stance per
    # event inside the 7d entry window (bet placed or PASS + gate reason)
    # ③ releases in the next 14 days — the "what's next" runway
    from prediction_market_macro.ops.ledger import open_positions as _open_pos
    open_bets = []
    for pos in _open_pos(conn):
        if (pos.get("ts_utc") or "") < TRACK_CUTOVER:
            continue                    # pre-cutover legacy stays internal
        st = json.loads(pos.get("structure_json") or "{}")
        mk = conn.execute(
            "SELECT ROUND(SUM(pnl_usd),4) s FROM marks WHERE decision_id=? AND"
            " ts=(SELECT MAX(ts) FROM marks WHERE decision_id=?)",
            (pos["id"], pos["id"])).fetchone()
        open_bets.append({
            "ts": pos["ts_utc"], "series": pos["series"], "period": pos["period"],
            "kind": pos["kind"], "desc": st.get("desc"), "fair": pos["fair"],
            "entry": pos["ask"], "size_usd": pos["size_usd"],
            "unrealized": mk["s"] if mk else None})
    open_bets.sort(key=lambda x: x["ts"] or "", reverse=True)
    stances = []
    for spec in REGISTRY.values():
        for r in conn.execute(
                "SELECT period, MAX(close_time) ct FROM contracts WHERE series=?"
                " AND status='active' GROUP BY period", (spec.ticker,)).fetchall():
            key = kalshi_period_to_key(r["period"])
            if not key or not r["ct"]:
                continue
            try:
                ct = datetime.fromisoformat(r["ct"].replace("Z", "+00:00"))
            except ValueError:
                continue
            dtc = (ct - now).total_seconds() / 86400.0
            if not (-0.5 <= dtc <= 7.5):
                continue
            de = _latest_decision(
                conn, spec.ticker, key,
                "ts_utc, kind, fair, ask, net_edge, size_usd, note, structure_json",
                include_close=False)         # #153: the standing actionable stance
            stj = json.loads(de["structure_json"] or "{}") if de else {}
            stances.append({
                "series": spec.ticker, "period": key, "close_ts": ct.isoformat(),
                "days_to_close": round(dtc, 1),
                "decision": ({"ts": de["ts_utc"], "kind": de["kind"],
                              "fair": de["fair"], "cost": de["ask"],
                              "net_edge": de["net_edge"], "size_usd": de["size_usd"],
                              "note": de["note"], "desc": stj.get("desc")}
                             if de else None)})
    stances.sort(key=lambda x: x["days_to_close"])
    upcoming = []
    for spec in REGISTRY.values():
        nr = cal.next_release(spec.calendar, now)
        if nr:
            days = (nr.scheduled_ts - now).total_seconds() / 86400.0
            if 0 <= days <= 14:
                upcoming.append({"series": spec.ticker, "period": nr.period,
                                 "scheduled_ts": nr.scheduled_ts.isoformat(),
                                 "days": round(days, 1)})
    upcoming.sort(key=lambda x: x["scheduled_ts"])
    _write(out_dir / "macro_bets.json",
           {"generated_at": now.isoformat(), "open_bets": open_bets,
            "stances": stances, "upcoming": upcoming})
    written.append("macro_bets.json")

    # ── macro_divergence.json: model vs market gap ranking per (series, period) ──
    from prediction_market_macro.strategy.devig import ladder_implied
    rows = []
    for spec in REGISTRY.values():
        for r in conn.execute(
                "SELECT DISTINCT period FROM contracts WHERE series=? AND status='active'",
                (spec.ticker,)).fetchall():
            key = kalshi_period_to_key(r["period"])
            if not key:
                continue
            pr = conn.execute(
                "SELECT asof, dist_json, ladder_json FROM preds WHERE series=?"
                " AND period=? AND model_version LIKE ? ORDER BY asof DESC LIMIT 1",
                (spec.ticker, key, spec.model + "/%")).fetchone()
            legs = [dict(x) for x in conn.execute(
                "SELECT c.ticker, c.floor_strike strike, q.yes_bid, q.yes_ask FROM contracts c"
                " LEFT JOIN quotes q ON q.ticker=c.ticker AND q.ts="
                " (SELECT MAX(ts) FROM quotes WHERE ticker=c.ticker)"
                " WHERE c.series=? AND c.period=? AND c.status='active'",
                (spec.ticker, r["period"])).fetchall()]
            if pr is None or not legs:
                continue
            gap = None
            if pr["ladder_json"]:
                import math as _m
                pmf = {float(k): v for k, v in json.loads(pr["ladder_json"]).items()}
                mean_model = sum(k * v for k, v in pmf.items())
                impl = ladder_implied(legs)
                if impl["strikes"]:
                    xs = impl["strikes"]
                    mass = impl["pmf"]
                    finite = {k: v for k, v in mass.items() if k != float("inf")}
                    if sum(finite.values()) > 0.3:
                        mean_mkt = sum(k * v for k, v in finite.items()) / sum(finite.values())
                        rng = max(xs) - min(xs) or 1.0
                        gap = abs(mean_model - mean_mkt) / rng
                        if not _m.isfinite(gap):
                            gap = None
            rows.append({"series": spec.ticker, "period": key, "asof": pr["asof"],
                         "gap_norm": round(gap, 4) if gap is not None else None,
                         "n_legs": len(legs)})
    rows.sort(key=lambda x: -(x["gap_norm"] or -1))
    _write(out_dir / "macro_divergence.json", {"generated_at": now.isoformat(), "rows": rows})
    written.append("macro_divergence.json")

    # ── macro_performance.json — presentation cutover 2026-07-31T16:14Z ──
    # Before the cutover the displayed record is the frozen hybrid-rule track
    # (experiments row 'track_history'); from the cutover on it is the actual
    # production ledger. Pre-cutover legacy positions (opened under the old,
    # pre-entry-window rules) are excluded from every displayed aggregate; the
    # append-only ledger keeps them internally (决策与盯市 shows the raw tail).
    from prediction_market_macro.ops import risk as _risk
    from prediction_market_macro.venues.kalshi.account import current_bankroll
    hist_row = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='track_history'"
        " ORDER BY created_ts DESC LIMIT 1").fetchone()
    history = json.loads(hist_row["metrics_json"]) if hist_row else None
    # #150. Open/closed and the realized figure both come from `ledger`, which pairs a
    # close to the POSITION it closed (#149's `closes_decision_id`, FIFO for legacy rows).
    # This used to ask "is there a `settle_note` on this (series, period) with a bigger
    # id?", which was wrong twice over: an EXIT is not a settle_note, so an exited
    # position stayed on the displayed open book forever with its realized PnL nowhere
    # (12 open / $9.55 staked / $0.00 realized shown against a true 8 / $6.39 / $+0.71);
    # and `LIMIT 1` pinned every position on a period to that period's FIRST settle_note,
    # so the 3 positions live on KXWTIW 2026-08-07 would have been credited one payout
    # three times over. A displayed track record is the last place to re-derive this.
    from prediction_market_macro.ops import ledger as _ledger
    closed_by = {d["id"]: d["close"] for d in _ledger.closures(conn)}
    live_open, live_settled = [], []
    for r in conn.execute(
            "SELECT * FROM decisions d WHERE d.kind IN ('open','argmax','arb','snipe')"
            " AND d.ts_utc>=? ORDER BY d.id", (TRACK_CUTOVER,)).fetchall():
        st = json.loads(r["structure_json"] or "{}")
        close = closed_by.get(r["id"])
        row = {"ts": r["ts_utc"], "day": r["ts_utc"][:10], "series": r["series"],
               "period": r["period"], "kind": r["kind"], "desc": st.get("desc"),
               "fair": r["fair"], "cost": r["ask"], "staked": r["size_usd"]}
        if close is not None:
            realized = _ledger.realized_usd(close)
            live_settled.append({**row, "realized": realized,
                                 # `realized is None` means the close row never recorded
                                 # the figure (42 of 53 live exits predate the field), not
                                 # that the trade broke even — so it is not a win, and the
                                 # aggregates below must exclude it rather than add 0.
                                 "won": realized is not None and realized > 0,
                                 "closed_by": close["kind"],
                                 "settle": close["ts_utc"][:10]})
        else:
            mk = conn.execute(
                "SELECT ROUND(SUM(pnl_usd),4) s FROM marks WHERE decision_id=? AND"
                " ts=(SELECT MAX(ts) FROM marks WHERE decision_id=?)",
                (r["id"], r["id"])).fetchone()
            live_open.append({**row, "unrealized": mk["s"] if mk else None})
    # #150. ROI is computed on the closures that actually recorded a realized figure, and
    # BOTH sides of the ratio use that same subset. Summing `realized or 0` over every
    # closure while `staked` counted all of them put unmeasured trades in the denominator
    # only, which drags ROI toward zero and reads as "we broke even on those" — the same
    # default-hides-a-hole shape as §25.20(b). `n_unrecorded` makes the gap visible
    # instead of silently absorbing it; it is 0 on the post-cutover book today.
    scored = [t for t in live_settled if t["realized"] is not None]
    ls_stk = sum(t["staked"] or 0 for t in scored)
    ls_rl = sum(t["realized"] for t in scored)
    live = {"since": TRACK_CUTOVER[:10],
            "settled": {"n_trades": len(live_settled),
                        "n_unrecorded": len(live_settled) - len(scored),
                        "won": sum(1 for t in live_settled if t["won"]),
                        "staked": round(ls_stk, 4), "realized": round(ls_rl, 4),
                        "roi": round(ls_rl / ls_stk, 5) if ls_stk else None,
                        "trades": live_settled[-60:]},
            "open": {"n": len(live_open),
                     "staked": round(sum(t["staked"] or 0 for t in live_open), 4),
                     "unrealized": round(sum(t["unrealized"] or 0
                                             for t in live_open), 4),
                     "positions": live_open[-60:]}}
    comb_n = (history["n_trades"] if history else 0) + len(live_settled)
    comb_w = (history["won"] if history else 0) + live["settled"]["won"]
    comb_stk = (history["staked"] if history else 0) + ls_stk
    comb_rl = (history["realized"] if history else 0) + ls_rl
    perf = {"generated_at": now.isoformat(),
            "track": {"cutover": TRACK_CUTOVER[:10], "history": history,
                      "live": live,
                      "combined": {"n_trades": comb_n, "won": comb_w,
                                   "staked": round(comb_stk, 4),
                                   "realized": round(comb_rl, 4),
                                   "roi": round(comb_rl / comb_stk, 5)
                                   if comb_stk else None}},
            "unrealized_usd": live["open"]["unrealized"],
            "bankroll_usd": current_bankroll(conn),
            "bankroll_source": "kalshi_demo", "mode": "paper"}
    _write(out_dir / "macro_performance.json", perf)
    written.append("macro_performance.json")

    # ── macro_oos.json — calibration replays ONLY (experiments also holds non-OOS
    # rows: 'bankroll' balance cache, 'dfm_gate' — those are not calibration cards) ──
    # scoring replays only (Brier cards) — decision_replay rows carry ROI metrics
    # and get their own section; latest row per (name, series)
    exps = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments e WHERE name LIKE '%replay%'"
        " AND name != 'decision_replay'"
        " AND created_ts=(SELECT MAX(created_ts) FROM experiments e2"
        "  WHERE e2.name=e.name AND COALESCE(e2.series,'')=COALESCE(e.series,''))"
        " ORDER BY e.series").fetchall()]
    dec_replays = []
    for r in conn.execute(
            "SELECT * FROM experiments e WHERE name='decision_replay'"
            " AND created_ts=(SELECT MAX(created_ts) FROM experiments e2"
            "  WHERE e2.name='decision_replay'"
            "  AND COALESCE(e2.series,'')=COALESCE(e.series,''))"
            " ORDER BY e.series").fetchall():
        try:
            m = json.loads(r["metrics_json"] or "{}")
        except json.JSONDecodeError:
            m = {}
        dec_replays.append({"series": r["series"], "n_trades": m.get("n_trades"),
                            "staked": m.get("staked"), "realized": m.get("realized"),
                            "roi": m.get("roi"), "edge_capture": m.get("edge_capture"),
                            "n_events": m.get("n_events")})
    gates = [dict(r) for r in conn.execute(
        "SELECT * FROM experiments WHERE name IN ('dfm_gate','series_gate')"
        " ORDER BY created_ts DESC LIMIT 40").fetchall()]
    wf_row = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name='daily_walkforward'"
        " ORDER BY created_ts DESC LIMIT 1").fetchone()
    walkforward = None
    if wf_row:
        try:
            w = json.loads(wf_row["metrics_json"])
            walkforward = {k: w.get(k) for k in
                           ("days", "n_trades", "win_rate", "staked", "realized",
                            "roi", "by_series", "lead_buckets", "curve", "note")}
        except json.JSONDecodeError:
            pass
    _write(out_dir / "macro_oos.json",
           {"generated_at": now.isoformat(), "experiments": exps,
            "decision_replays": dec_replays, "component_gates": gates,
            "walkforward": walkforward,
            "gate_note": "brier_model must beat brier_market before real money"
                         " (paper until then)"})
    written.append("macro_oos.json")

    # ── macro_risk.json ──
    riskdoc = {"generated_at": now.isoformat(), "limits": _risk.LIMITS,
               "scenario": _risk.scenario_var(conn),
               "open_exposure": _risk._open_exposure(conn)}
    _write(out_dir / "macro_risk.json", riskdoc)
    written.append("macro_risk.json")

    # ── macro_pricetrack.json: intraday mark history (mother price-track port) ──
    track = [dict(r) for r in conn.execute(
        "SELECT m.ts, ROUND(SUM(m.pnl_usd),4) pnl_usd, COUNT(*) n_legs FROM marks m"
        " JOIN decisions d ON d.id=m.decision_id AND d.ts_utc>=?"
        " GROUP BY m.ts ORDER BY m.ts DESC LIMIT 500", (TRACK_CUTOVER,)).fetchall()]
    track.reverse()
    per_series = [dict(r) for r in conn.execute(
        "SELECT d.series, ROUND(SUM(m.pnl_usd),4) pnl_usd FROM marks m"
        " JOIN decisions d ON d.id=m.decision_id AND d.ts_utc>=?"
        " WHERE m.ts=(SELECT MAX(ts) FROM marks) GROUP BY d.series",
        (TRACK_CUTOVER,)).fetchall()]
    _write(out_dir / "macro_pricetrack.json",
           {"generated_at": now.isoformat(), "track": track,
            "latest_by_series": per_series})
    written.append("macro_pricetrack.json")

    # ── PDF reports → public/data/macro_reports/ + index (0-bis whitelist (a):
    # "macro_*.json 与 macro PDF") — makes the Reports tile servable via /data ──
    import shutil
    rep_src = settings.output_dir / "reports"
    rep_dst = out_dir / "macro_reports"
    reports = []
    if rep_src.exists():
        rep_dst.mkdir(exist_ok=True)
        # renderer-fix cutoff (2026-07-31: bullet-marker bug + raw alert dumps):
        # pre-fix PDFs stay on disk as archives but leave the panel — every report
        # is a snapshot of the same DB, nothing is lost by not listing them
        _min_mtime = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc).timestamp()
        pdfs = sorted((p for p in rep_src.glob("*.pdf")
                       if p.stat().st_mtime >= _min_mtime),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:14]
        for p in pdfs:
            dst = rep_dst / p.name
            if not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime:
                shutil.copy2(p, dst)
            reports.append({"name": p.name, "url": f"/data/macro_reports/{p.name}",
                            "mtime": datetime.fromtimestamp(
                                p.stat().st_mtime, tz=timezone.utc).isoformat(),
                            "kind": "weekly" if "weekly" in p.name else "daily"})
    _write(out_dir / "macro_reports.json",
           {"generated_at": now.isoformat(), "reports": reports})
    written.append("macro_reports.json")

    # ── macro_walkforward.json: the WF lab artifact (sweep + latest daily run) ──
    wf_doc = {"generated_at": now.isoformat()}
    # 'daily'/'daily60' filter on window explicitly — the latest daily_walkforward
    # row depends on run order (sweep's per-lead runs, 60d vs 30d), never rely on it
    for name, key, wfilter in (("walkforward_sweep", "sweep", None),
                               ("daily_walkforward", "daily", "30d%"),
                               ("daily_walkforward", "daily60", "60d%"),
                               ("ml_selector", "ml", None)):
        q = "SELECT metrics_json, created_ts FROM experiments WHERE name=?"
        args = [name]
        if wfilter:
            q += " AND window LIKE ?"
            args.append(wfilter)
        r = conn.execute(q + " ORDER BY created_ts DESC LIMIT 1", args).fetchone()
        if r:
            try:
                wf_doc[key] = json.loads(r["metrics_json"])
                wf_doc[key + "_ts"] = r["created_ts"]
            except json.JSONDecodeError:
                pass
    _write(out_dir / "macro_walkforward.json", wf_doc)
    written.append("macro_walkforward.json")

    # ── macro_fed.json: meeting-level detail with evidence chain ──
    # decided meetings leave the probability board (a settled decision has no
    # probabilities to show — the outcome lives in decisions/performance views)
    from prediction_market_macro.util.periods import kalshi_period_to_key as _k2k
    settled_meets = {_k2k(r["period"]) for r in conn.execute(
        "SELECT DISTINCT period FROM settlements WHERE series='KXFEDDECISION'"
        " AND result IN ('yes','no')").fetchall()}
    settled_meets.discard(None)
    meetings = []
    for r in conn.execute(
            "SELECT period, asof, dist_json, inputs_json FROM preds"
            " WHERE series='KXFEDDECISION' AND model_version LIKE 'fed/%'"
            " AND asof=(SELECT MAX(asof) FROM preds p2 WHERE p2.series='KXFEDDECISION'"
            " AND p2.model_version LIKE 'fed/%'"
            " AND p2.period=preds.period) ORDER BY period").fetchall():
        if r["period"] in settled_meets:
            continue
        meetings.append({"period": r["period"], "asof": r["asof"],
                         "probs": json.loads(r["dist_json"]).get("probs"),
                         "inputs": json.loads(r["inputs_json"] or "{}")})
    _write(out_dir / "macro_fed.json",
           {"generated_at": now.isoformat(), "meetings": meetings})
    written.append("macro_fed.json")
    return ",".join(written)
