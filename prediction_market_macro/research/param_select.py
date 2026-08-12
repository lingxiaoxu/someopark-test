"""research/param_select.py — the production side of #119: choose today's parameters.

`param_wf` answers "would rolling re-selection have helped, historically". This is the
thing that actually runs: once a day, for every series, rebuild the trailing PnL score
matrix from events that closed strictly earlier, hand it to `dsr.select`, and write the
verdict to `param_selection`. `predict_all` then reads one row per series.

**What it optimises, and why not Brier.**
Realised dollars of the live hybrid rule, via `pnl_score` — the same objective `param_wf`
uses, for the reason set out in `DECISION_RULE_119.md`: a ladder has 10-20 legs and the
strategy touches exactly one, so mean per-leg Brier can improve without a single bet
changing. That is not a theoretical worry. Measured on KXAAAGASW, all **73** sets of the
`aaa_*` grid produce byte-identical realised PnL ($8.38 over 11 events) while their Brier
scores differ — an entire grid that a Brier-scored selector would rank confidently and that
cannot move a dollar.

**The gate is the product — but it is currently a gate that can never open.** On every day
so far this returns the registered defaults: the candle archive admits 63 tradeable events
in total and no single series reaches `dsr.MIN_OBS` = 12.

An earlier version of this docstring argued that was only a matter of waiting, because
`com.someopark.macroweekly` backfills weekly and "each weekly series gains about one event a
week". That claim was measured on 2026-08-05 (§25.7 / #138) and it is **wrong**. Kalshi
candles expire at ~75 days (#120) and nothing archives them, so a series does not accumulate
— it slides. Steady-state event count is (retention / cadence), which is:

    weekly ~10.7   monthly ~2.5   fomc ~1.7   quarterly ~0.8      vs MIN_OBS = 12

Every cadence sits below the floor, and the weekly series are already AT their ceiling
(KXWTIW / KXNATGASW / KXAAAGASW hold 10-11 events, oldest 72-74d; real candle bars start
2026-05-16, 81 days back, while settlements go back to 2021). They gain one a week and lose
one a week: **net accrual is zero, forever.** `params_adopted: 0` is therefore not a young
sample, it is a fixed point.

Do not respond by lowering MIN_OBS — 12 is already thin for DSR and the whole point of the
deflation is that a small n cannot support a 73-way grid search. The only thing that changes
this is starting #120's archival cron: with history retained rather than expiring, the weekly
series cross 12 in about two weeks. Until then this module is a correctly-built mechanism
attached to a supply that does not exist, and it should be described that way rather than as
imminent.

**Why there is a cache, and what invalidates it.**
Scoring one series is a full replay of the decision stack for every event x every set —
minutes, not milliseconds. But the answer cannot change until a new scoreable event settles,
so the sample is fingerprinted (count + latest close) and an unchanged fingerprint carries
yesterday's verdict forward as a fresh row. Weekly series therefore recompute about once a
week and monthly ones about once a month, while `predict_all` still gets a row every day.

The fingerprint is deliberately NOT a hash of the parameters or of the code. A code change
that alters predictions will not invalidate it, so `refresh(force=True)` is what to run
after touching a model — the alternative is hashing the model source, which would recompute
every series on every unrelated edit and make the daily job unaffordable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from prediction_market_macro.research import dsr as _dsr
from prediction_market_macro.research.param_space import build_grid, settled_events
from prediction_market_macro.research.param_wf import (MODULE_OF, POOLS, _predict_fn,
                                                       build_matrix)

OBJECTIVE = "pnl"

# Series whose selection runs on a pooled branch sample instead of their own events.
# Populated from POOLS so the two cannot drift apart. Enabling a pool is a real decision
# and is recorded in DECISION_RULE_119.md — a pool changes WHICH events a series is judged
# on, and pooling across a branch boundary would difference against parameters that cannot
# move half the sample. `POOLS` is already restricted to same-branch members.
POOLED_SERIES = {s: name for name, spec in POOLS.items() for s in spec["series"]}


def _fingerprint(conn, series: str, before: datetime) -> str:
    """(count, latest close) of the scoreable sample — cheap, and it moves iff the sample
    does. Two SELECT-side numbers rather than the event list itself, because this is read
    on every series on every daily run."""
    from prediction_market_macro.research import pnl_score
    evs = pnl_score.quotable_events(conn, series, before=before)
    last = max((e["close_ts"] for e in evs), default=None)
    return f"{len(evs)}:{last.isoformat() if last else '-'}"


def _sample_key(conn, series: str, before: datetime) -> str:
    if series in POOLED_SERIES:
        name = POOLED_SERIES[series]
        parts = [f"{s}={_fingerprint(conn, s, before)}"
                 for s in POOLS[name]["series"]]
        return f"pool:{name}|" + "|".join(parts)
    return _fingerprint(conn, series, before)


def _matrix_for(conn, series: str, before: datetime, log=None):
    """(grid, kept_events, matrix) for the sample this series is judged on.

    Pooled series are scored against the branch's shared grid on the branch's combined
    events, merged in close order. Un-pooled series use their own grid and their own
    events. Either way the matrix rows are LOSSES (negative dollars).
    """
    if series in POOLED_SERIES:
        spec = POOLS[POOLED_SERIES[series]]
        members, probe = spec["series"], spec["probe"]
        n = sum(len(settled_events(conn, s, before=before)) for s in members)
        grid, report = build_grid(conn, probe, spec["module"], _predict_fn(probe), n,
                                  log=log)
        if len(grid) <= 1:
            return [{}], [], [], report
        grid = [{}] + grid
        kept, mat = [], []
        for s in members:
            k, m = build_matrix(conn, s, grid, OBJECTIVE, before, log=log)
            kept.extend(k)
            mat.extend(m)
        order = sorted(range(len(kept)), key=lambda i: kept[i]["close"])
        return grid, [kept[i] for i in order], [mat[i] for i in order], report
    n = len(settled_events(conn, series, before=before))
    grid, report = build_grid(conn, series, MODULE_OF[series], _predict_fn(series), n,
                              log=log)
    if len(grid) <= 1:
        return [{}], [], [], report
    grid = [{}] + grid
    kept, mat = build_matrix(conn, series, grid, OBJECTIVE, before, log=log)
    return grid, kept, mat, report


def manual_params(conn, series: str, before: datetime) -> tuple[dict, str] | None:
    """User-adopted parameter override, PIT-gated on its own adoption timestamp.

    2026-08-11: the user directed adoption of the 75-day sweep argmin winners into
    production per series, over the DSR gate's objection (every market sat below
    MIN_OBS; the objection is recorded in README §E and the adoption in the row's
    note field). The override is a row, not a code change, so it is reversible by
    `clear_manual` and auditable in `experiments`. The PIT clause matters for the
    walkforward: a simulated day BEFORE the adoption instant uses the normal
    selector path, so historical replays are not contaminated by today's decision.
    """
    r = conn.execute(
        "SELECT metrics_json, created_ts FROM experiments WHERE name='manual_params'"
        " AND series=? ORDER BY created_ts DESC LIMIT 1", (series,)).fetchone()
    if r is None:
        return None
    m = json.loads(r["metrics_json"] or "{}")
    if not m.get("active") or before.isoformat() < r["created_ts"]:
        return None
    return dict(m.get("params") or {}), r["created_ts"]


def set_manual(conn, series: str, params: dict, note: str) -> None:
    """Each write is a NEW row (timestamp in the key): the sequence of rows IS the
    parameter change history the user asked to keep — `manual_params` reads the
    latest, `history()` reads them all, nothing is ever overwritten."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('manual_params',?,?,?,?,?)",
        (f"manual:{series}:{now}", series, "live",
         json.dumps({"active": True, "params": params, "note": note}), now))
    conn.commit()


def clear_manual(conn, series: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO experiments(name, config_hash, series, window,"
        " metrics_json, created_ts) VALUES('manual_params',?,?,?,?,?)",
        (f"manual:{series}:{now}", series, "live",
         json.dumps({"active": False, "params": {}, "note": "cleared"}), now))
    conn.commit()


def history(conn, series: str | None = None) -> list[dict]:
    """The parameter change log, oldest first."""
    q = ("SELECT series, metrics_json, created_ts FROM experiments"
         " WHERE name='manual_params'")
    args: tuple = ()
    if series:
        q += " AND series=?"
        args = (series,)
    out = []
    for r in conn.execute(q + " ORDER BY created_ts", args):
        m = json.loads(r["metrics_json"] or "{}")
        out.append({"series": r["series"], "ts": r["created_ts"],
                    "active": m.get("active"), "params": m.get("params"),
                    "note": (m.get("note") or "")[:200]})
    return out


def params_asof(conn, series: str, asof: datetime) -> dict:
    """The params that were IN FORCE when something was computed at `asof` — the
    read a determinism replay must use. Mirrors what `current()` returned at that
    wall-clock moment: the manual override if it had been adopted by then (PIT on
    the adoption timestamp), else that day's param_selection row, else defaults.
    Added 2026-08-12 after health's replay canary re-predicted at DEFAULTS,
    mismatched every adopted-params pred, went red on four series and force-exited
    all three live positions 49 minutes before the CPI print."""
    mp = manual_params(conn, series, asof)
    if mp is not None:
        return dict(mp[0])
    # NOT current(): its manual check uses wall-clock now, which would leak
    # today's adoption into a pre-adoption asof. Read the day's row directly.
    d = asof.date().isoformat()
    floor = (asof.date() - timedelta(days=MAX_STALE_DAYS)).isoformat()
    row = conn.execute(
        "SELECT params_json FROM param_selection WHERE series=? AND day<=? AND day>=?"
        " ORDER BY day DESC LIMIT 1", (series, d, floor)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["params_json"]) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def select_for(conn, series: str, before: datetime, adopt_p: float = _dsr.ADOPT_P,
               log=None) -> tuple[dict, dict]:
    """(params, report) for one series as of `before`. `params` is {} for the defaults.

    Everything read here closed strictly before `before`, so a selection stamped for day D
    never saw day D's own settle. That is the same PIT contract `param_wf` replays under,
    which is what makes the historical comparison predictive of this.
    """
    mp = manual_params(conn, series, before)
    if mp is not None:
        params, since = mp
        return params, {"series": series, "adopted": True, "chosen": "manual",
                        "mode": "manual", "n_obs": None,
                        "asof": before.isoformat(),
                        "reason": f"manual_params row active since {since}"}
    grid, kept, mat, grid_report = _matrix_for(conn, series, before, log=log)
    rep = {"series": series, "objective": OBJECTIVE, "asof": before.isoformat(),
           "pool": POOLED_SERIES.get(series), "n_sets": len(grid),
           "grid_report": grid_report}
    if len(grid) <= 1 or not kept:
        rep.update({"adopted": False, "chosen": 0, "n_obs": len(kept),
                    "reason": grid_report.get("reason", "no grid — defaults only")})
        return {}, rep
    cols = {j: [row[j] for row in mat] for j in range(len(grid))}
    sel = _dsr.select(cols, 0, adopt_p=adopt_p)
    rep.update(sel)
    # Dollars are what a human reads this table for; the matrix is carried negated.
    rep["pnl_default"] = round(-sum(cols[0]), 4)
    rep["pnl_chosen"] = round(-sum(cols[sel["chosen"]]), 4)
    return (dict(grid[sel["chosen"]]) if sel["adopted"] else {}), rep


def refresh(conn, asof: datetime | None = None, series: list[str] | None = None,
            adopt_p: float = _dsr.ADOPT_P, force: bool = False, log=print) -> dict:
    """Write one `param_selection` row per series for today. Returns {series: params}.

    A series whose sample fingerprint is unchanged since its last row is carried forward
    rather than rescored — see the module docstring. `force=True` rescores everything and
    is what to run after changing a model.
    """
    now = asof or datetime.now(timezone.utc)
    day = now.date().isoformat()
    todo = series or list(MODULE_OF)
    out = {}
    for s in todo:
        try:
            key = _sample_key(conn, s, now)
            # `day<=` and not `day<`: the daily pipeline can run more than once, and a row
            # already written today was computed from the same or an earlier `asof`, so
            # reusing it is PIT-safe. The fingerprint still governs — an event that settled
            # between the two runs moves the key and forces a rescore.
            prev = conn.execute(
                "SELECT params_json, adopted, n_obs, n_trials, dsr_p, report_json"
                " FROM param_selection WHERE series=? AND day<=? ORDER BY day DESC LIMIT 1",
                (s, day)).fetchone()
            reused = False
            if prev is not None and not force:
                pr = json.loads(prev["report_json"])
                if pr.get("sample_key") == key:
                    params = json.loads(prev["params_json"])
                    rep = {**pr, "asof": now.isoformat(), "carried_forward": True}
                    reused = True
            if not reused:
                params, rep = select_for(conn, s, now, adopt_p=adopt_p, log=None)
                rep["sample_key"] = key
                rep["carried_forward"] = False
            conn.execute(
                "INSERT OR REPLACE INTO param_selection(series, day, params_json,"
                " adopted, n_obs, n_trials, dsr_p, report_json, created_ts)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (s, day, json.dumps(params, sort_keys=True),
                 1 if rep.get("adopted") else 0, rep.get("n_obs"), rep.get("n_trials"),
                 rep.get("dsr_p"), json.dumps(rep, default=str), now.isoformat()))
            out[s] = params
            if log:
                log(f"  {s}: {'ADOPTED ' + json.dumps(params, sort_keys=True) if params else 'defaults'}"
                    f" ({'carried' if rep.get('carried_forward') else 'rescored'};"
                    f" n_obs={rep.get('n_obs')} p={rep.get('dsr_p')})")
        except Exception as e:                                   # noqa: BLE001
            # A selector failure must never take the daily prediction run down with it:
            # no row means `current()` falls back, and the alert is the thing that gets
            # looked at. Silently returning {} here would be indistinguishable from a
            # legitimate "the gate held".
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "error", "param_select", f"{s}: {e}"))
            if log:
                log(f"  {s}: FAILED {type(e).__name__}: {e}")
    conn.commit()
    return out


MAX_STALE_DAYS = 30


def current(conn, series: str, day: str | None = None) -> dict:
    """Today's parameters for one series — one SELECT, safe to call in the predict loop.

    Falls back to the most recent row within `MAX_STALE_DAYS` when today's has not been
    written, because a selection computed from strictly-earlier events stays PIT-valid and
    a missed daily run should not silently flip the model back to defaults mid-week. Past
    that horizon it does return defaults: a month-old selection is a stale artefact, and
    quietly running on it would be worse than running on what is registered.
    """
    # manual_params outranks the table here exactly as it does in select_for — this is
    # the read `predict_all`/ticks actually make, and on 2026-08-11 the adopted params
    # silently missed a whole morning of production predictions because the fingerprint
    # carry-forward in `refresh` copied a pre-adoption row and only THIS function was
    # consulted. One override, every door, or the override is a lie.
    mp = manual_params(conn, series, datetime.now(timezone.utc))
    if mp is not None:
        return dict(mp[0])
    d = day or datetime.now(timezone.utc).date().isoformat()
    floor = (datetime.fromisoformat(d).date() - timedelta(days=MAX_STALE_DAYS)).isoformat()
    row = conn.execute(
        "SELECT params_json FROM param_selection WHERE series=? AND day<=? AND day>=?"
        " ORDER BY day DESC LIMIT 1", (series, d, floor)).fetchone()
    if row is None:
        return {}
    try:
        return json.loads(row["params_json"]) or {}
    except Exception:                                            # noqa: BLE001
        return {}


def main():
    import argparse
    from prediction_market_macro.config.settings import load_settings
    from prediction_market_macro.ingest.store import init_db
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*")
    ap.add_argument("--asof")
    ap.add_argument("--adopt-p", type=float, default=_dsr.ADOPT_P)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    conn = init_db(load_settings().db_path)
    asof = (datetime.fromisoformat(a.asof).replace(tzinfo=timezone.utc)
            if a.asof else None)
    res = refresh(conn, asof=asof, series=a.series, adopt_p=a.adopt_p, force=a.force)
    print(json.dumps({k: v for k, v in res.items() if v} or
                     {"adopted": "none — all series on registered defaults"}, indent=1))


if __name__ == "__main__":
    main()
