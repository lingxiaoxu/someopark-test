"""research/prereg.py — the weekly caller the three preregistered graders never had.

The audit finding this closes: `shadow_claims` / `shadow_pr2` / `shadow_s2` each judge
themselves at a preregistered sample threshold (8 weeks / 20 legs / 30 trades), but no
scheduled job ever ran them — so the day a registration matures, nobody is told, and a
matured-but-unread verdict slowly turns into "we kept trading a rule the data had
already rejected" (or the mirror image). This module makes maturation an ALERT, not a
thing someone has to remember.

It changes nothing about the registrations themselves: graders stay the single source
of verdict wording, thresholds stay in docs/PREREGISTER.md (pinned by their own tests),
and this caller only READS — run() here writes nothing but the alert row on a
non-PENDING verdict. Alerting is edge-triggered against the last alert text per
registration, so a matured verdict alerts once, not weekly forever.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

REGS = ("pr1_claims", "pr2_argmax", "pr7_s2")


def _graders():
    from prediction_market_macro.research import shadow_claims, shadow_pr2, shadow_s2
    return {"pr1_claims": shadow_claims.run, "pr2_argmax": shadow_pr2.run,
            "pr7_s2": shadow_s2.run}


def run_all(conn) -> dict:
    """→ {reg: verdict-head}; inserts one alert per registration whose verdict is no
    longer PENDING and whose text differs from the last alert we filed for it."""
    out = {}
    for name, grade in _graders().items():
        try:
            verdict = str(grade(conn).get("verdict", "?"))
        except Exception as e:                              # noqa: BLE001
            verdict = f"GRADER ERROR: {e}"
        out[name] = verdict.split("—")[0].strip()[:40]
        if verdict.startswith("PENDING"):
            continue
        msg = f"PREREG {name}: {verdict[:300]}"
        last = conn.execute(
            "SELECT message FROM alerts WHERE source='prereg' AND message LIKE ?"
            " ORDER BY ts DESC LIMIT 1", (f"PREREG {name}:%",)).fetchone()
        if last and last["message"] == msg:
            continue
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (datetime.now(timezone.utc).isoformat(), "info", "prereg", msg))
        conn.commit()
    return out


def main():
    from pathlib import Path
    from prediction_market_macro.ingest.store import connect

    db = Path(__file__).resolve().parent.parent / "data" / "macro.db"
    print(json.dumps(run_all(connect(db)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
