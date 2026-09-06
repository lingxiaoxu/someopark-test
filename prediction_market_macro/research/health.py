"""research/health.py — daily model health patrol (PLAN §9.6). Runs inside refresh.

Checks:
  1. data freshness per source (FRED age vs cadence, quotes age)
  2. pred freshness + ladder mass sanity + replay determinism per series
  3. break detectors (§9.6-2, any trigger ⇒ series red + circuit breaker):
     a) rolling Brier behind the market 2 consecutive replay windows
     b) CRPS rolling mean beyond own 12-run mean + 2σ (suddenly dumber)
     c) entropy convergence: <48h to release yet newer pred entropy RISES
     d) feature out-of-bounds: any first-print diff beyond its 5y z=4 envelope
     e) Chronos bridge: NaN / crossed quantiles in the latest shadow pred
  4. ledger self-check: 3 date-seeded random open decisions replayed from their own
     inputs_json — fair must match bit-for-bit (silent-code-change canary)
  5. red lights auto-trip ops.risk.circuit_breaker (→ decide_all blocks new opens,
     exits force-closes, exec stays locked) — 铁律 10.
Output: red/yellow/green per series → macro_health.json + alerts on red.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY, effective_strike_type
from prediction_market_macro.ops.predict_all import SERIES_DISPATCH


def _replay_name(series: str) -> str:
    return "claims_replay" if series == "KXJOBLESSCLAIMS" else f"{series[2:].lower()}_replay"


def _detect_brier_2win(conn, series: str) -> str | None:
    """(a) model behind market in the 2 most recent replay windows."""
    rows = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name=? AND series=?"
        " ORDER BY created_ts DESC LIMIT 2", (_replay_name(series), series)).fetchall()
    if len(rows) < 2:
        return None
    behind = 0
    for r in rows:
        m = json.loads(r["metrics_json"])
        bm, bk = m.get("brier_model-1h"), m.get("brier_market-1h")
        if bm is not None and bk is not None and bm > bk:
            behind += 1
    return "brier_behind_market_2win" if behind == 2 else None


def _detect_crps_spike(conn, series: str) -> str | None:
    """(b) latest replay CRPS beyond own 12-run mean + 2σ."""
    rows = conn.execute(
        "SELECT metrics_json FROM experiments WHERE name=? AND series=?"
        " ORDER BY created_ts DESC LIMIT 13", (_replay_name(series), series)).fetchall()
    vals = []
    for r in rows:
        c = json.loads(r["metrics_json"]).get("crps-1h")
        if c is not None:
            vals.append(float(c))
    if len(vals) < 6:
        return None
    latest, hist = vals[0], vals[1:]
    mu = sum(hist) / len(hist)
    sd = math.sqrt(sum((x - mu) ** 2 for x in hist) / max(len(hist) - 1, 1))
    if sd > 0 and latest > mu + 2 * sd:
        return f"crps_spike:{latest:.1f}>mu{mu:.1f}+2sd{sd:.1f}"
    return None


def _entropy(dist: dict) -> float | None:
    probs = None
    if isinstance(dist.get("probs"), dict):
        probs = list(dist["probs"].values())
    elif isinstance(dist.get("pmf"), dict):
        probs = list(dist["pmf"].values())
    if not probs:
        return None
    return -sum(p * math.log(max(p, 1e-12)) for p in probs if p > 0)


def _detect_entropy_rise(conn, spec, now: datetime) -> str | None:
    """(c) inside 48h of a release the pred distribution should be converging
    (entropy non-increasing between the two latest preds)."""
    rel = conn.execute(
        "SELECT period, scheduled_ts FROM releases WHERE cal=? AND scheduled_ts>?"
        " ORDER BY scheduled_ts LIMIT 1", (spec.calendar, now.isoformat())).fetchone()
    if rel is None:
        return None
    ts = datetime.fromisoformat(rel["scheduled_ts"])
    if (ts - now).total_seconds() > 48 * 3600:
        return None
    rows = conn.execute(
        "SELECT ladder_json, dist_json FROM preds WHERE series=? AND period=?"
        " AND model_version LIKE ? ORDER BY asof DESC LIMIT 2",
        (spec.ticker, rel["period"], spec.model + "/%")).fetchall()
    if len(rows) < 2:
        return None
    ents = []
    for r in rows:
        src = json.loads(r["ladder_json"]) if r["ladder_json"] else None
        e = _entropy({"pmf": src} if src else json.loads(r["dist_json"]))
        if e is None:
            return None
        ents.append(e)
    newer, older = ents[0], ents[1]
    if newer > older + 0.05:
        return f"entropy_rise:{older:.3f}->{newer:.3f}<48h"
    return None


def _detect_feature_oob(conn, now: datetime) -> list[str]:
    """(d) latest first-print diff of every ingested FRED sid vs its 5y z=4 envelope."""
    flags = []
    cut5y = (now - timedelta(days=5 * 365)).date().isoformat()
    sids = [r["sid"] for r in conn.execute(
        "SELECT sid, COUNT(DISTINCT event_time) n FROM fred_obs WHERE event_time>=?"
        " GROUP BY sid HAVING n>=60", (cut5y,)).fetchall()]
    for sid in sids:
        rows = conn.execute(
            "SELECT event_time, value, MIN(knowledge_time) kt FROM fred_obs"
            " WHERE sid=? AND event_time>=? GROUP BY event_time ORDER BY event_time",
            (sid, cut5y)).fetchall()
        vals = [float(r["value"]) for r in rows]
        if len(vals) < 60:
            continue
        diffs = [b - a for a, b in zip(vals[:-1], vals[1:])]
        latest, hist = diffs[-1], diffs[:-1]
        mu = sum(hist) / len(hist)
        sd = math.sqrt(sum((x - mu) ** 2 for x in hist) / max(len(hist) - 1, 1))
        if sd > 0 and abs(latest - mu) / sd > 4.0:
            flags.append(f"feature_oob:{sid}:z={(latest - mu) / sd:.1f}")
    return flags


def _detect_chronos(conn, series: str) -> str | None:
    """(e) latest chronos2 shadow pred: quantiles must be finite and monotone."""
    r = conn.execute(
        "SELECT dist_json FROM preds WHERE series=? AND model_version LIKE 'chronos2%'"
        " ORDER BY asof DESC LIMIT 1", (series,)).fetchone()
    if r is None:
        return None
    d = json.loads(r["dist_json"])
    qs = d.get("quantiles") or d.get("values")
    if not isinstance(qs, list) or not qs:
        return None
    try:
        xs = [float(x) for x in qs]
    except (TypeError, ValueError):
        return "chronos_bad_values"
    if any(math.isnan(x) or math.isinf(x) for x in xs):
        return "chronos_nan"
    if any(b < a for a, b in zip(xs[:-1], xs[1:])):
        return "chronos_quantile_crossing"
    return None


# Series whose label can be fused with the settled ladder, chosen by MEASUREMENT rather
# than by the old assertion (#216). Admission is all three of: 100.0% per-leg agreement
# over the whole settled history — no floor below 1.0, because a breaker expected to
# misfire is a breaker that gets disabled the first morning it fires — at least 8 labelled
# periods, and zero disagreements inside the window the breaker actually reads.
#
# Measured 2026-08-28 over the live db, per-leg agreement:
#   KXJOBLESSCLAIMS 492/492   KXU3 455/455   KXPAYROLLS 350/350
#   KXFED           314/314   KXAAAGASW 118/118            -> admitted
#   KXWTIW      2205/2213 (.9964)  KXCPIYOY .9624  KXCPICOREYOY .9645
#   KXCPI       .9474  KXCPICORE .9439  KXPCECORE .9262      -> excluded, and the old
# comment's reason is confirmed for the CPI/PCE family: the label is a raw MoM float
# while Kalshi settles on the PUBLISHED value rounded to the contract unit, so a print of
# 0.2081 against a T0.2 rung reads YES and settled NO. That is a real ~1e-2 disagreement,
# far above the 1e-5 transport band `_leg_expected` declines, and it must keep them out.
# KXWTIW's residual is the known CL front-month roll. KXNATGASW and KXFEDDECISION produce
# no labelled legs at all (no label / 'custom' strikes) and would fuse vacuously.
# KXGDP passes on 9 legs from 1 period — untested, not narrowly passing.
# AAA left the excluded list because the 2026-08-27 fix moved `_realized_print` off the
# EIA weekly pump average and onto the AAA daily national average it settles on.
_FUSE_SERIES = ("KXJOBLESSCLAIMS", "KXU3", "KXPAYROLLS", "KXFED", "KXAAAGASW")

# PER SERIES, not across them. With one shared `LIMIT 120` the window is won by whoever
# settles most often: KXAAAGASW posts a ~34-leg ladder DAILY, so on the five-series set it
# takes 87 of 120 rows and evicts KXU3 to exactly ZERO — a widening that reads as more
# coverage and silently removes the breaker from a series it already guarded, collapsing
# the window's span from two months to seventeen days. Per-series, no cadence can crowd
# out another and 120 is the same depth each fused series has today: claims goes 96 -> 120
# legs and U3 24 -> 120. Cost of the whole check on the live db is 33ms against 13ms.
_FUSE_PER_SERIES = 120


# Some strikes in the stored book sit one part in 1e-6 BELOW their nominal lattice value:
# `KXU3-25FEB-T4.1` has floor_strike = 4.099999. On a strict-greater ladder that unit in
# the last place is not cosmetic — it moves the exactly-at-strike case from NO to YES,
# which is the whole of 铁律 2. Measured over the live db: 123 settled legs across
# KXU3 / KXCPIYOY / KXCPICOREYOY / KXWTIW carry such a strike, every one of them LOW, and
# every one off by exactly 1.000e-06. Five KXU3 legs print exactly on the nominal strike;
# Kalshi settled all five NO while a raw comparison against 4.099999 says YES.
#
# This is a HISTORICAL encoding, not one still on the wire: KXU3's `.1` rungs are
# 4.099999 for every period from 23JAN through 25JUN and clean 4.1 for all fourteen
# periods from 25JUL on, including the three currently active. The guard therefore
# defends the stored book — which is what the breaker reads, and which a re-ingest or a
# window change can pull back into range — rather than tomorrow's fetch.
#
# The separation from a DELIBERATE offset is six orders of magnitude and must be kept:
# KXPAYROLLS strikes are stored at 99999 / 149999 / -1, exactly 1.0 below the lattice,
# because Kalshi writes ">= 100,000" as "> 99,999". That is semantics, not transport, and
# it must survive untouched. Any threshold in [1e-6, 1.0) separates the two; this is ten
# times the observed offset.
_STRIKE_EPS = 1e-5


def _leg_expected(y: float, strike_type: str | None, floor, cap,
                  default_strict: bool) -> str | None:
    """The label this leg SHOULD have carried, or None when it cannot be said.

    None already means "no honest expectation here" to both callers — they skip it. Two
    guards route into that same answer rather than into a wrong one:

    (1) A MISSING bound used to raise. That is not cosmetic either: `_settle_label_check`
        is a global breaker, and a breaker that raises does not flag a mismatch, it takes
        the whole 06:00 health run down with a stack trace. 604 settled legs in the live
        db carry `strike_type IS NULL` together with `floor_strike IS NULL` (the
        pre-2025-02 deep backfill, where Kalshi's older payloads had no strike fields) —
        all currently far outside the newest-120 window, but the shape reappears whenever
        a settlement lands before its contract row is filled in. `strategy/snipe.py`
        reaches it sooner: it back-fills `strike` from `cap_strike` for its own None-check
        and then passes the original, still-None `strike` in here under `greater*`.

    (2) A print sitting a STRICTLY POSITIVE but sub-quantum distance from the bound is
        not judged. `0 < |y - bound| <= _STRIKE_EPS` is the signature of transport loss
        and nothing else: the bound is not the number Kalshi settled on, so neither
        answer is defensible, and this function's caller escalates a wrong answer to a
        production halt. Exact equality is deliberately NOT in the band — `|y - bound|
        == 0` is a real, decidable case, and it is the case `strict_gt` exists to decide.
        KXJOBLESSCLAIMS strikes sit on a 250 lattice with integer prints, so landing
        exactly on one is ordinary, and `greater_or_equal` calls it YES correctly; a
        blanket "near the line" guard would swallow that, silently retire `strict_gt`
        altogether, and cost the breaker real coverage on the series it was built for.

        It DECLINES rather than snapping the strike to the lattice: the five observed
        cases would all snap correctly, but snapping asserts that every sub-lattice
        offset is transport noise, and KXPAYROLLS is standing proof that some are meant.
        Declining costs 5 checks out of 9663 and asserts nothing. Legs missed this way
        are not dropped from the world — `strategy/snipe.py` already refuses to trade a
        far wider band (`BOUNDARY_FRAC`, half a grid step) around the same line.
    """
    def _ulp(bound) -> bool:
        return 0.0 < abs(y - bound) <= _STRIKE_EPS

    st = strike_type or ("greater" if default_strict else "greater_or_equal")
    # 2026-09-02: within the greater-family the TIE rule comes from the series' registry
    # flag, not from the contract's nominal type. Kalshi labels KXAAAGASW legs 'greater'
    # and settled the first exact tie (26AUG31-4.080, print 4.080) YES; reading the
    # contract's word literally made this check call a correct settlement a mismatch and
    # trip the global breaker two mornings running. The contract type still selects the
    # family (greater / less / between); the registry, set by observed settlements,
    # decides what happens ON the line.
    if st in ("greater", "greater_or_equal"):
        st = "greater" if default_strict else "greater_or_equal"
        if floor is None or _ulp(floor):
            return None
        return "yes" if (y > floor if st == "greater" else y >= floor) else "no"
    if st in ("less", "less_or_equal"):
        if cap is None or _ulp(cap):
            return None
        return "yes" if (y < cap if st == "less" else y <= cap) else "no"
    if st == "between" and floor is not None and cap is not None:
        if _ulp(floor) or _ulp(cap):
            return None
        return "yes" if floor <= y <= cap else "no"
    return None


def _settle_label_check(conn, now: datetime) -> list[str]:
    """铁律 2 cross-check: settled leg YES/NO must agree with the first-print label
    vs its strike, honoring the leg's own strike_type. Restricted to series whose
    label is directly in contract units (_FUSE_SERIES). A mismatch means our
    settlement understanding (or the label pipe) is wrong — global breaker."""
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.ops.pnl import _realized_print
    from prediction_market_macro.util.periods import kalshi_period_to_key
    bad = []
    rows = []
    for _s in _FUSE_SERIES:                      # one budget each — see _FUSE_PER_SERIES
        rows += conn.execute(
            "SELECT s.series, s.period, s.ticker, s.result, c.floor_strike,"
            " c.cap_strike, c.strike_type FROM settlements s"
            " JOIN contracts c ON c.ticker=s.ticker"
            " WHERE s.result IN ('yes','no') AND s.series=?"
            " ORDER BY s.settled_ts DESC LIMIT ?", (_s, _FUSE_PER_SERIES)).fetchall()
    cache: dict[tuple[str, str], float | None] = {}
    for r in rows:
        key = kalshi_period_to_key(r["period"]) if r["period"] else None
        if not key:
            continue
        ck = (r["series"], key)
        if ck not in cache:
            cache[ck] = _realized_print(conn, r["series"], key)
        y = cache[ck]
        if y is None:
            continue
        expected = _leg_expected(y, r["strike_type"], r["floor_strike"],
                                 r["cap_strike"], REGISTRY[r["series"]].strict_gt)
        if expected is not None and expected != r["result"]:
            bad.append(f"settle_label_mismatch:{r['ticker']}:label={y}"
                       f" strike={r['floor_strike']}/{r['cap_strike']}"
                       f" expect={expected} got={r['result']}")
    return bad[:10]


def _late_data_after(conn, asof: str, created_ts: str) -> list[str]:
    """Rows knowable at `asof` (knowledge_time <= asof) that we only RECEIVED after the
    pred was written (first_seen_ts > created_ts). Such rows are visible to a PIT
    re-prediction and were invisible to the original — the honest explanation for a
    replay diff that has nothing to do with the code. Returns 'ROOT:date' / 'SID:date'
    tags, newest first, empty when nothing arrived late."""
    out: list[str] = []
    try:
        for r in conn.execute(
                "SELECT root, event_time FROM fut_daily WHERE knowledge_time <= ?"
                " AND first_seen_ts > ? ORDER BY event_time DESC LIMIT 5",
                (asof, created_ts)):
            out.append(f"{r[0]}:{r[1]}")
        for r in conn.execute(
                "SELECT sid, event_time FROM fred_obs WHERE knowledge_time <= ?"
                " AND first_seen_ts > ? ORDER BY event_time DESC LIMIT 5",
                (asof, created_ts)):
            out.append(f"{r[0]}:{r[1]}")
    except Exception:                                            # noqa: BLE001
        return []                     # a broken lookup must not decide a breaker
    return out


def _ledger_selfcheck(conn, now: datetime, k: int = 3) -> list[str]:
    """(4) k date-seeded random open decisions: recompute fair from the row's own
    inputs_json dist + structure legs — mismatch means code silently changed."""
    from prediction_market_macro.model.common import leg_fair
    rows = conn.execute(
        "SELECT id, series, ts_utc, structure_json, inputs_json, fair FROM decisions"
        " WHERE kind='open' AND fair IS NOT NULL").fetchall()
    if not rows:
        return []
    rng = random.Random(now.date().isoformat())
    sample = rng.sample(list(rows), min(k, len(rows)))
    bad = []
    for d in sample:
        try:
            st = json.loads(d["structure_json"])
            if st.get("kind") != "single" or not st.get("legs"):
                continue                                   # only singles replay offline
            inp = json.loads(d["inputs_json"])
            leg = st["legs"][0]
            probs = inp.get("probs") if isinstance(inp.get("probs"), dict) else None
            pmf = ({float(kk): v for kk, v in inp["pmf"].items()}
                   if isinstance(inp.get("pmf"), dict) else None)
            if probs is not None:
                fair = float(probs.get(leg["ticker"].rsplit("-", 1)[-1], 0.0))
            elif pmf is not None:
                c = conn.execute(
                    "SELECT floor_strike, cap_strike, strike_type FROM contracts"
                    " WHERE ticker=?", (leg["ticker"],)).fetchone()
                if c is None or c["floor_strike"] is None:
                    continue
                # the tie rule IN FORCE when this decision was recorded (registry
                # history) — a rule corrected later must not make an older, correct
                # decision look like silently changed code (2026-09-02, KXAAAGASW)
                fair = leg_fair(pmf, effective_strike_type(d["series"], c["strike_type"],
                                                           asof=d["ts_utc"]),
                                c["floor_strike"], c["cap_strike"])
            else:
                continue                                   # dist form not replayable
            if leg["side"] == "no":
                fair = 1 - fair
            # decisions.fair is the CALIBRATED value the gates consumed — replay
            # must walk through the same map or a live calibration map would
            # false-trip the global breaker on every selfcheck
            from prediction_market_macro.strategy import calibration as _cal
            fair = _cal.apply(conn, d["series"], fair)
            if abs(fair - float(d["fair"])) > 1e-6:
                bad.append(f"ledger_replay_mismatch:id={d['id']}"
                           f":{fair:.6f}!={d['fair']:.6f}")
        except Exception as e:                            # noqa: BLE001
            bad.append(f"ledger_replay_error:id={d['id']}:{e}")
    return bad


def daily_health(conn, settings) -> str:
    now = datetime.now(timezone.utc)
    report: dict = {"ts": now.isoformat(), "sources": {}, "series": {}, "flags": []}

    # 1. source freshness
    for sid, max_age_d in (("ICSA", 9), ("CPIAUCSL", 40), ("DFEDTARU", 5),
                           ("UNRATE", 40), ("GDPNOW", 12),
                           ("AAA_DAILY", 3), ("NG_STORAGE_WEEKLY", 9),
                           ("GASOLINE_STOCKS_WEEKLY", 9),
                           ("CRUDE_STOCKS_WEEKLY", 9)):
        r = conn.execute("SELECT MAX(knowledge_time) m FROM fred_obs WHERE sid=?",
                         (sid,)).fetchone()
        age = None
        if r["m"]:
            age = (now - datetime.fromisoformat(r["m"])).days
        report["sources"][sid] = {"latest_kt": r["m"], "age_days": age}
        if age is None or age > max_age_d:
            report["flags"].append(f"stale_source:{sid}:{age}d")
    q = conn.execute("SELECT MAX(ts) m FROM quotes").fetchone()
    q_age_h = (now - datetime.fromisoformat(q["m"])).total_seconds() / 3600 if q["m"] else None
    report["sources"]["kalshi_quotes"] = {"age_hours": round(q_age_h, 1) if q_age_h else None}
    if q_age_h is None or q_age_h > 26:
        report["flags"].append(f"stale_quotes:{q_age_h}")

    # 2+3. per-series: pred freshness, ladder mass, replay determinism
    import importlib
    for spec in REGISTRY.values():
        s_rep = {"status": "green", "notes": []}
        # replay the PRODUCTION model's latest pred only — shadow members (chronos2/*)
        # have their own model_version and would trivially mismatch the dispatch fn
        pr = conn.execute(
            "SELECT * FROM preds WHERE series=? AND model_version LIKE ?"
            " ORDER BY asof DESC LIMIT 1",
            (spec.ticker, spec.model + "/%")).fetchone()
        if pr is None:
            s_rep = {"status": "yellow", "notes": ["no_preds_yet"]}
        else:
            age_h = (now - datetime.fromisoformat(pr["asof"])).total_seconds() / 3600
            if age_h > 26:
                s_rep["status"] = "red"
                s_rep["notes"].append(f"pred_stale:{age_h:.0f}h")
            if pr["ladder_json"]:
                mass = sum(json.loads(pr["ladder_json"]).values())
                if abs(mass - 1.0) > 0.01:
                    s_rep["status"] = "red"
                    s_rep["notes"].append(f"ladder_mass:{mass:.3f}")
            # replay determinism (dependency drift canary). MUST re-predict with the
            # params that were in force at the pred's asof — 2026-08-12 this replayed
            # at registered defaults, mismatched every adopted-params pred, went red
            # on four series and the breaker force-exited all three live positions 49
            # minutes before the CPI print. A determinism check that ignores half the
            # inputs is a false-positive generator, not a canary.
            disp = SERIES_DISPATCH.get(spec.ticker)
            if disp:
                try:
                    from prediction_market_macro.research.param_select import params_asof
                    mod = importlib.import_module(disp[0])
                    fn = getattr(mod, disp[1])
                    _asof = datetime.fromisoformat(pr["asof"])
                    re_pred = fn(conn, _asof, pr["period"],
                                 series=spec.ticker,
                                 params=(params_asof(conn, spec.ticker, _asof) or None))
                    if re_pred.model_version != pr["model_version"]:
                        # deploy rollover: the stored pred predates a version bump, so a
                        # dist diff is EXPECTED, not drift. Note it, stay green — the next
                        # tick writes a pred at the new version and the canary re-arms.
                        # (Same incident class as 2026-08-12: a determinism check fed
                        # mismatched inputs is a false-positive generator.)
                        s_rep["notes"].append(
                            f"replay_skip_version:{pr['model_version']}"
                            f"->{re_pred.model_version}")
                    elif json.dumps(re_pred.dist.to_json()) != pr["dist_json"]:
                        # 2026-09-05: NG's 09-03 bar reached us on 09-05 (yfinance served
                        # a broken row on 09-04 and the completed-bar rule rightly dropped
                        # it). Every pred written on 09-04 was computed without it; the
                        # re-prediction here, filtering on knowledge_time <= asof, SAW it —
                        # and called the difference model drift. It is not: it is data
                        # that arrived after the pred was written. Third such episode
                        # since 08-01 (08-03, 08-28, 09-03 — all four roots, always +2d),
                        # so the provider does this ~12% of days. Late data is a vintage
                        # event and gets its own note; only a diff that late data cannot
                        # explain is drift.
                        late = _late_data_after(conn, pr["asof"], pr["created_ts"])
                        if late:
                            s_rep["notes"].append(
                                "replay_skip_late_data:" + ",".join(late[:3]))
                        else:
                            s_rep["status"] = "red"
                            s_rep["notes"].append("replay_mismatch")
                except Exception as e:                            # noqa: BLE001
                    s_rep["status"] = "red"
                    s_rep["notes"].append(f"replay_error:{e}")
        # §9.6-2 break detectors a/b/c/e (d is source-level, runs once below)
        for det in (_detect_brier_2win(conn, spec.ticker),
                    _detect_crps_spike(conn, spec.ticker),
                    _detect_entropy_rise(conn, spec, now),
                    _detect_chronos(conn, spec.ticker)):
            if det:
                s_rep["status"] = "red"
                s_rep["notes"].append(det)
        report["series"][spec.ticker] = s_rep
        if s_rep["status"] == "red":
            # 铁律 10 nuance: QUALITY reds (model losing to the market) demote to
            # paper — which is already where we are; paper must keep trading or the
            # OOS sample that could ever pass the gate stops accruing. Only
            # INTEGRITY reds (data/code/pipeline broken — even paper output is
            # garbage) trip the breaker that halts paper decisions too.
            quality = {"brier_behind_market_2win"}
            integrity_notes = [n for n in s_rep["notes"]
                               if n.split(":")[0] not in quality
                               and not n.startswith("crps_spike")
                               and not n.startswith("entropy_rise")]
            # quality-only red = expected steady state pre-gate → warn (dashboard
            # amber), not a screaming error; integrity red = error + breaker.
            level = "error" if integrity_notes else "warn"
            msg = f"RED {spec.ticker}: {s_rep['notes']}"
            dup = conn.execute(
                "SELECT 1 FROM alerts WHERE source='health' AND message=? AND ts>=?",
                (msg, now.date().isoformat())).fetchone()
            if not dup:
                conn.execute(
                    "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                    (now.isoformat(), level, "health", msg))
            if integrity_notes:
                from prediction_market_macro.ops import risk
                risk.circuit_breaker(conn, spec.ticker,
                                     "health_red:" + ",".join(integrity_notes)[:180])

    # 4. rolling OOS from the latest replay experiment
    ex = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name='claims_replay'"
        " ORDER BY created_ts DESC LIMIT 1").fetchone()
    if ex:
        m = json.loads(ex["metrics_json"])
        report["oos_claims"] = m
        if (m.get("brier_model-1h") or 0) > (m.get("brier_market-1h") or 1):
            report["series"]["KXJOBLESSCLAIMS"]["notes"].append(
                "oos_brier_behind_market — stays paper (gate)")

    # detector (d): source-level feature out-of-bounds envelope
    oob = _detect_feature_oob(conn, now)
    report["flags"].extend(oob)
    for f in oob:
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "health", f))

    # (4) ledger self-check: 3 date-seeded random decisions
    bad = _ledger_selfcheck(conn, now)
    report["ledger_selfcheck"] = {"n": 3, "mismatches": bad}
    for b in bad:
        report["flags"].append(b)
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "health", b))
        from prediction_market_macro.ops import risk
        risk.circuit_breaker(conn, "*", b[:180])

    # 铁律 2: settlement ↔ first-print label reconciliation
    slb = _settle_label_check(conn, now)
    for b in slb:
        report["flags"].append(b)
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "health", b))
        from prediction_market_macro.ops import risk
        risk.circuit_breaker(conn, "*", b[:180])

    # Self-healing release: every breaker reason raised ABOVE this line was re-evaluated
    # from scratch in this run, so any older breaker holding a reason this run did NOT
    # raise is stale and is released here. Placed after every detector and before the
    # rolling-20 breaker, which is deliberately not self-healing (ops/risk.py explains).
    from prediction_market_macro.ops import risk as _risk0
    # INDIVIDUAL notes and flags, never joined: the breaker stores a comma-joined note
    # list and release_resolved splits it back, so the two sides must be comparable at
    # the level of one condition. Joining here let a live condition be released whenever
    # a resolved one happened to sort ahead of it.
    live_reasons = {str(f) for f in report["flags"]}
    for v in report["series"].values():
        live_reasons |= {str(n) for n in (v.get("notes") or [])}
    freed = _risk0.release_resolved(conn, live_reasons, now)
    report["breakers_released"] = freed

    # rolling-20 realized-PnL breaker (PLAN §12)
    from prediction_market_macro.ops import risk as _risk
    r20 = _risk.check_rolling20(conn)
    if r20:
        report["flags"].append(r20)

    conn.commit()
    path = settings.output_dir / "macro_health.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    reds = [k for k, v in report["series"].items() if v["status"] == "red"]
    return f"{len(report['series'])} series, red={reds or 'none'}, flags={len(report['flags'])}"
