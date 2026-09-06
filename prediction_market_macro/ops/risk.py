"""ops/risk.py — exposure limits + circuit breaker (PLAN §12, §9.6-4).

check() is called by decide_all BEFORE recording an open; a Veto turns the decision into
a pass with the veto reason in the ledger note. Clusters: all contracts of the same
(family, period) count together (CPI MoM/YoY/COMBO of one print move together).

circuit_breaker(): trips a series (or '*' for global) by writing an alerts row with
source='circuit_breaker'. Consumers: exec.trading_allowed (blocks real orders 7d),
decide_all (blocks NEW paper opens 24h), exits.run (forces position exit). Trip paths:
health red lights (research/health.py §9.6-4) and the rolling-20 realized-PnL check.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY

LIMITS = {
    "per_event_usd": 5.0,       # one (series, period)
    "per_family_usd": 20.0,
    "per_cluster_usd": 8.0,     # same (family, period) across series — correlated prints
                                # (plan said $40; kept at $8 deliberately — more
                                # conservative until any series passes the gate)
    "per_corr_cluster_usd": 8.0,  # PR-29: exposure across series whose JOINT-LAW settle
                                  # correlation with the candidate is >= CORR_CLUSTER_MIN,
                                  # regardless of family. One-sided by construction; inert
                                  # when data/synth/portfolio_corr.json is absent.
    "per_release_day_usd": 30.0,  # total NEW exposure opened per calendar day (§12)
    "gross_usd": 100.0,
}
CORR_CLUSTER_MIN = 0.5           # frozen with the limit by PR-29; changes need a new PR


def _corr_matrix() -> dict:
    # The weekly-refreshed joint-law correlation pairs, or {} when absent/unreadable.
    # {} makes the corr-cluster cap a no-op: the registered degradation direction — this
    # cap may only ever remove risk, so its failure mode must be "no cap", never "wrong
    # cap".
    import json
    from prediction_market_macro.config.settings import load_settings
    try:
        p = load_settings(require_keys=False).db_path
        raw = json.loads((p.parent / "synth" / "portfolio_corr.json").read_text())
        return {k: float(v) for k, v in raw.get("corr", {}).items()}
    except Exception:                                              # noqa: BLE001
        return {}


def _corr(pairs: dict, a: str, b: str) -> float:
    return pairs.get(f"{a}|{b}", pairs.get(f"{b}|{a}", 0.0))


@dataclass(frozen=True)
class Veto:
    reason: str


def _open_exposure(conn) -> list[dict]:
    """What the desk currently has at risk — the input to every exposure cap below.

    #150. Was `NOT EXISTS (a close on this period with a bigger id)`, i.e. "is there ANY
    later close here", which is not a count: on a period holding several positions, one
    close hid all of them. It happened to agree with the truth on the live book (both
    give 8 positions / $6.39), so this is a latent fix, not a live one — but a caps
    function that under-reads exposure fails open, and this was the last private copy of
    the rule #149 consolidated into `ledger.open_decisions`.
    """
    from prediction_market_macro.ops import ledger as _ledger
    return [{"series": d["series"], "period": d["period"], "size_usd": d["size_usd"]}
            for d in _ledger.open_positions(conn)]


def check(conn, series: str, period: str, size_usd: float) -> Veto | None:
    fam = REGISTRY[series].family if series in REGISTRY else "other"
    month = period[:7]
    ev = fam_ex = cl = gross = 0.0
    for p in _open_exposure(conn):
        s = p["size_usd"] or 0.0
        gross += s
        p_fam = REGISTRY[p["series"]].family if p["series"] in REGISTRY else "other"
        if p["series"] == series and p["period"] == period:
            ev += s
        if p_fam == fam:
            fam_ex += s
            if p["period"][:7] == month:
                cl += s
    if ev + size_usd > LIMITS["per_event_usd"]:
        return Veto(f"risk_per_event {ev + size_usd:.2f}>{LIMITS['per_event_usd']}")
    if fam_ex + size_usd > LIMITS["per_family_usd"]:
        return Veto(f"risk_per_family {fam_ex + size_usd:.2f}>{LIMITS['per_family_usd']}")
    if cl + size_usd > LIMITS["per_cluster_usd"]:
        return Veto(f"risk_cluster {cl + size_usd:.2f}>{LIMITS['per_cluster_usd']}")
    # PR-29: the joint-law generalization of the cluster cap — series whose generated
    # settle summaries co-move (same panel draw, coupled bridges, pins) count together
    # whatever their family. Reads the weekly matrix; inert without it.
    pairs = _corr_matrix()
    if pairs:
        corr_ex = sum((p["size_usd"] or 0.0) for p in _open_exposure(conn)
                      if p["series"] != series
                      and _corr(pairs, series, p["series"]) >= CORR_CLUSTER_MIN)
        if corr_ex + size_usd > LIMITS["per_corr_cluster_usd"]:
            return Veto(f"risk_corr_cluster {corr_ex + size_usd:.2f}"
                        f">{LIMITS['per_corr_cluster_usd']}")
    if gross + size_usd > LIMITS["gross_usd"]:
        return Veto(f"risk_gross {gross + size_usd:.2f}>{LIMITS['gross_usd']}")
    # #151/F7. Every cap above reads `_open_exposure` -> `ledger.open_positions`, which
    # counts all four OPEN_KINDS. This one counted `kind='open'` alone, so an argmax/arb/
    # snipe leg consumed none of the daily budget — the same "one question, two kind-sets"
    # split #149 and #150 fixed on the close side, surviving inside a single function.
    # It fails OPEN (under-reads today's exposure, so it lets trades through), which is
    # the dangerous direction for a cap. Latent today: argmax is 4 rows / $3.19 lifetime
    # and the biggest mixed day is 2026-08-05 at $6.10 against a $30 cap. Note the two
    # days that DID breach it — 07-28 ($48) and 07-29 ($32) — are NOT this bug: both are
    # pure `kind='open'` (mixed-kind total == open-only total), so the cap they broke is
    # the one that was already being counted. Those 80 rows are the book's first two days
    # and start at id 1; what let them through is a separate question, still open.
    today = datetime.now(timezone.utc).date().isoformat()
    from prediction_market_macro.ops.ledger import OPEN_KINDS
    opened_today = conn.execute(
        f"SELECT COALESCE(SUM(size_usd),0) s FROM decisions WHERE kind IN"
        f" ({','.join('?' * len(OPEN_KINDS))}) AND ts_utc>=?",
        (*OPEN_KINDS, today)).fetchone()["s"]
    if opened_today + size_usd > LIMITS["per_release_day_usd"]:
        return Veto(f"risk_release_day {opened_today + size_usd:.2f}"
                    f">{LIMITS['per_release_day_usd']}")
    return None


def circuit_breaker(conn, series: str, reason: str) -> None:
    """Trip the breaker for `series` ('*' = global). Idempotent per (series, reason,
    day): re-tripping the same day is a no-op so daily health runs don't spam."""
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    msg = f"{series}: {reason}"
    dup = conn.execute(
        "SELECT 1 FROM alerts WHERE source='circuit_breaker' AND message=? AND ts>=?"
        " AND acked=0", (msg, day)).fetchone()
    if dup:
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (now.isoformat(), "error", "circuit_breaker", msg))
    conn.commit()


def breaker_tripped(conn, series: str, within_hours: float = 24.0) -> str | None:
    """Reason string if `series` (or the global '*') tripped within the window.
    Acked alerts are released — acking a circuit_breaker alert IS the manual
    operator release (人工复核才复活, 铁律 10)."""
    cut = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
    r = conn.execute(
        "SELECT message FROM alerts WHERE source='circuit_breaker' AND ts>=? AND"
        " acked=0 AND (message LIKE ? OR message LIKE '*:%')"
        " ORDER BY ts DESC LIMIT 1",
        (cut, f"{series}:%")).fetchone()
    return r["message"] if r else None


# Trip reasons whose CONDITION is re-evaluated from scratch on every health run, so a
# breaker holding one of them can be released the moment the condition is gone. The
# rolling-20 PnL breaker is deliberately NOT here: a drawdown does not stop being real
# because the next run recomputes it, and releasing it needs a human.
SELF_HEALING_PREFIXES = ("health_red:", "settle_label_mismatch:", "ledger_selfcheck:")


def _breaker_notes(msg: str) -> list[str]:
    """The individual conditions a breaker message asserts.

    `health_red:` carries a COMMA-JOINED note list, so it must be split back into notes
    before anything is compared: comparing the joined string against the live one is what
    made the first version of this function release a live breaker whenever the resolved
    note happened to sort first. Other reasons are single flags carried verbatim.
    """
    body = msg.split(":", 1)[1].lstrip() if ":" in msg else msg
    if body.startswith("health_red:"):
        notes = [n for n in body[len("health_red:"):].split(",") if n]
        # informational notes (replay_skip_*, between_listings, ...) can appear inside a
        # breaker string written before health learned to keep them out; they assert
        # nothing, so they must not hold the breaker either.
        from prediction_market_macro.research.health import INFORMATIONAL_PREFIXES
        return [n for n in notes if not n.startswith(INFORMATIONAL_PREFIXES)]
    return [body] if body else []


def _still_live(note: str, live: set[str]) -> bool:
    """Conservative: a note counts as still live if it matches any live flag OR is a
    prefix of one (breaker messages are truncated at 180 chars, live flags are not), or
    vice versa. Ambiguity keeps the breaker CLOSED — a risk control must fail safe."""
    return any(l == note or l.startswith(note) or note.startswith(l) for l in live)


def release_resolved(conn, live: set[str], now: datetime | None = None) -> list[str]:
    """Ack breakers whose conditions the CURRENT health run no longer reports.

    2026-09-02, after the KXAAAGASW tie incident: the settle-label rule was corrected the
    same day, `_settle_label_check` went clean immediately — and the desk still sat blocked,
    because a tripped breaker holds for 24h on the alerts row alone and nothing re-read the
    condition. Two mornings of blocked opens and three force-exited positions were bought by
    a bug that had already been fixed. A breaker is a statement about the CURRENT state of
    the system; when the state is verifiably good again, holding the block is itself a
    defect.

    `live` is the set of INDIVIDUAL flags/notes this run raised. A breaker is released only
    when EVERY condition it asserts is absent from that set — one live condition holds the
    whole row. Only self-healing reasons are eligible; the rolling-20 PnL breaker never is.

    The ack is the existing manual-release mechanism (铁律 10) — the alert row survives as
    an audit record, and an `info` row names what was released and why, so a reader can
    always tell an auto-release from an operator one.

    No time window: an unacked breaker whose condition is gone should be acked whatever its
    age (rows older than `breaker_tripped`'s 24h are already inert for blocking, but leaving
    them unacked leaves a permanently red row for a resolved condition).
    """
    now = now or datetime.now(timezone.utc)
    # `id` is this table's INTEGER PRIMARY KEY, i.e. the rowid alias — selecting `rowid`
    # returns a column NAMED "id", so r["rowid"] raises. Ask for the real column.
    # level='error' keeps the scan off the info audit rows this function writes itself.
    rows = conn.execute(
        "SELECT id, message FROM alerts WHERE source='circuit_breaker'"
        " AND level='error' AND acked=0").fetchall()
    released = []
    for r in rows:
        rid, msg = r["id"], r["message"]
        body = msg.split(":", 1)[1].lstrip() if ":" in msg else msg
        if not body.startswith(SELF_HEALING_PREFIXES):
            continue
        notes = _breaker_notes(msg)
        if not notes or any(_still_live(n, live) for n in notes):
            continue
        conn.execute("UPDATE alerts SET acked=1 WHERE id=?", (rid,))
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now.isoformat(), "info", "circuit_breaker",
             f"auto-release: condition no longer reported by the health run — {msg[:150]}"))
        released.append(msg)
    if released:
        conn.commit()
    return released


def check_rolling20(conn) -> str | None:
    """PLAN §12: rolling-20-closure drawdown breaker — settlements AND early exits
    both count (a string of exit losses must be able to trip it).

    #150. Closures come from `ledger.closures`, one per POSITION. The old query deduped
    settles by `(series, period)` while counting exits raw, which was wrong in both
    directions at once: several positions settling on one period collapsed to a single
    closure, and the 16 settle_note rows the pre-#149 settle_pass wrote for positions it
    had ALREADY settled each counted as an independent one (-6.07 of settle rows against
    -4.37 of settled positions).

    The breaker has never armed. 42 of the 53 live exits predate `realized_usd`, and they
    used to be dropped from the window silently — leaving 18 closures against a required
    20, so this returned None as if all were well. Missing data now shortens the window
    LOUDLY (an `alerts` row), because a risk control that switches itself off when it
    cannot see is worse than one that is absent: the absent one is at least visible.
    """
    from prediction_market_macro.ops import ledger as _ledger
    scored, unrecorded = [], 0
    # settle_note + exit only. `cancel` is NOT a closure: those 43 rows are #121's
    # administrative retirement of the disavowed pre-cutover book, retired by rule and
    # explicitly excluded from the displayed track. Feeding them to a drawdown breaker
    # would let a bookkeeping action move a risk control.
    for p in _ledger.closures(conn, ("settle_note", "exit")):
        rz = _ledger.realized_usd(p["close"])
        if rz is None:
            unrecorded += 1
        else:
            scored.append((p["close"]["id"], rz))
    scored.sort(key=lambda x: -x[0])
    rows = scored[:20]
    if len(rows) < 20:
        if unrecorded:
            _breaker_blind(conn, len(rows), unrecorded)
        return None
    total = sum(p for _, p in rows)
    thresh = -2.0 * LIMITS["per_event_usd"]
    if total < thresh:
        reason = f"rolling20_pnl {total:.2f} < {thresh:.2f}"
        circuit_breaker(conn, "*", reason)
        return reason
    return None


def _breaker_blind(conn, n_scored: int, n_unrecorded: int) -> None:
    """Say out loud that the rolling-20 breaker is short of data, once per day.

    Deliberately NOT a `circuit_breaker` row: it must not halt trading (铁律 10 reserves
    that for a measured drawdown), and it must not need an operator ack to clear — it
    clears itself as soon as 20 scored closures exist. It exists so that "the breaker
    returned None" can be told apart from "the breaker had nothing to look at".
    """
    msg = (f"rolling20 disarmed: {n_scored}/20 scored closures"
           f" ({n_unrecorded} closes carry no realized_usd)")
    today = datetime.now(timezone.utc).date().isoformat()
    dup = conn.execute("SELECT 1 FROM alerts WHERE source='risk' AND message=?"
                       " AND ts>=? LIMIT 1", (msg, today)).fetchone()
    if dup:
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(), "warn", "risk", msg))
    conn.commit()


def scenario_var(conn) -> dict:
    """Baseline: independent stake-sum worst case. Upgrades itself to the DFM joint
    scenario engine once model/dfm_bridge passes its §7-bis adoption gate (weekly
    gate_check) and the scenario cache is fresh — reversible: gate FAIL or stale cache
    falls straight back here."""
    try:
        from prediction_market_macro.model.dfm_bridge import scenario_var_dfm
        joint = scenario_var_dfm(conn)
    except Exception:                             # noqa: BLE001 — bridge is optional
        joint = None
    worst = 0.0
    for p in _open_exposure(conn):
        worst += p["size_usd"] or 0.0            # binary max loss = stake (paper $1 cap)
    base = {"max_loss_all_events_usd": round(worst, 2), "mode": "independent_stake_sum"}
    if joint is not None:
        return {**joint, "max_loss_all_events_usd": base["max_loss_all_events_usd"]}
    return base
