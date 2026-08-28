"""research/prereg.py — the weekly caller the preregistered graders never had.

The audit finding this closes: `shadow_claims` / `shadow_pr2` / `shadow_s2` /
`shadow_seasonal` each judge themselves at a preregistered sample threshold (8 weeks /
20 legs / 30 trades / 6 FIRING weeks), but no scheduled job ever ran them — so the day a registration matures, nobody is told, and a
matured-but-unread verdict slowly turns into "we kept trading a rule the data had
already rejected" (or the mirror image). This module makes maturation an ALERT, not a
thing someone has to remember.

It changes nothing about the registrations themselves: graders stay the single source
of verdict wording, thresholds stay in docs/PREREGISTER.md (pinned by their own tests),
and this caller only READS — run() here writes nothing but the alert row on a
non-PENDING verdict. Alerting is edge-triggered against the last alert text per
registration, so a matured verdict alerts once, not weekly forever.

Second channel (`PREREG-CODE`): the replay scorers pin a `REGISTERED_FINGERPRINT` of the
model file they grade, and say so in their report when it moves undocumented. That note
is worthless if the only field ever read is the verdict — and the moment it matters is
mid-forward-window, when the verdict is still PENDING and the maturation channel is
deliberately silent. So a changed-and-undocumented fingerprint alerts on its own prefix,
at `warn`, whatever the verdict says.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

REGS = ("pr1_claims", "pr2_argmax", "pr7_s2",
        "pr8_nowcast_yoy", "pr8_nowcast_core", "pr10_nowcast_mom",
        "pr11_seasonal", "pr12_width_natgas", "pr12_width_wti", "pr13_otm_sell")


def _graders():
    """→ {registration: conn -> dict carrying "verdict"}.

    Not one entry per MODULE — one per thing that matures. `shadow_nowcast` grades three
    (PR-8 headline, PR-8 core, PR-10) and `shadow_width` grades one per registered
    market, and they mature on unrelated calendars: PR-10's first forward event is
    2026-09-11, PR-12's twelfth settled week is months out, PR-11's sixth FIRING week is
    ~2027-04. Folding any of them together would make the slowest one gate the alert for
    the fastest. PR-8's core gets its own line for a second reason: its wiring was a user
    decision on a measured tie, so its verdict cannot be allowed to hide inside the
    headline's, which is the one that has evidence behind it.

    `_once` makes that fan-out free: these two scorers are REPLAYS (a predict per event
    per arm), and the weekly line should pay for each exactly once, not once per verdict
    read off it. Failures are cached too — a scorer that dies on a missing table should
    not be retried three times to produce the same three GRADER ERROR lines.
    """
    from prediction_market_macro.research import (shadow_claims, shadow_nowcast,
                                                  shadow_otm, shadow_pr2, shadow_s2,
                                                  shadow_seasonal, shadow_width)
    cache: dict = {}

    def _once(mod):
        def call(conn):
            if mod.__name__ not in cache:
                try:
                    cache[mod.__name__] = (mod.run(conn), None)
                except Exception as e:                      # noqa: BLE001
                    cache[mod.__name__] = (None, e)
            rep, err = cache[mod.__name__]
            if err is not None:
                raise err
            return rep
        return call

    def otm(conn):
        r = shadow_otm.run(conn)
        return {**r, "code": r["code_change"]}

    nowcast, width = _once(shadow_nowcast), _once(shadow_width)
    return {
        "pr1_claims": shadow_claims.run,
        "pr2_argmax": shadow_pr2.run,
        "pr7_s2": shadow_s2.run,
        "pr8_nowcast_yoy": lambda c: {"verdict": nowcast(c)["pr8"]["verdict"],
                                      "code": nowcast(c)["code"]},
        "pr8_nowcast_core": lambda c: {"verdict": nowcast(c)["pr8"]["core_verdict"],
                                       "code": nowcast(c)["code"]},
        "pr10_nowcast_mom": lambda c: {"verdict": nowcast(c)["pr10"]["verdict"],
                                       "code": nowcast(c)["code"]},
        "pr11_seasonal": shadow_seasonal.run,
        "pr12_width_natgas": lambda c: {**width(c)["series"]["KXNATGASW"],
                                        "code": width(c)["code_change"]},
        "pr12_width_wti": lambda c: {**width(c)["series"]["KXWTIW"],
                                     "code": width(c)["code_change"]},
        # PR-13 reads only the book, so it is a query rather than a replay and needs no
        # `_once`. Its fingerprint tracks `strategy/edge.py` (the FEE schedule) instead of
        # a model file — see the module docstring for why that is the thing that can
        # silently void the paired comparison here.
        "pr13_otm_sell": otm,
    }


def _file_once(conn, msg: str, like: str, level: str = "info") -> None:
    """Edge-triggered against the last alert matching `like` — so a standing condition
    alerts on the pass it appears and then goes quiet, instead of weekly forever."""
    last = conn.execute(
        "SELECT message FROM alerts WHERE source='prereg' AND message LIKE ?"
        " ORDER BY ts DESC LIMIT 1", (like,)).fetchone()
    if last and last["message"] == msg:
        return
    conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                 (datetime.now(timezone.utc).isoformat(), level, "prereg", msg))
    conn.commit()


def run_all(conn) -> dict:
    """→ {reg: verdict-head}; inserts one alert per registration whose verdict is no
    longer PENDING and whose text differs from the last alert we filed for it."""
    out = {}
    for name, grade in _graders().items():
        code, warn = {}, None
        try:
            rep = grade(conn)
            verdict = str(rep.get("verdict", "?"))
            code = rep.get("code") or {}
            warn = rep.get("data_warning")
        except Exception as e:                              # noqa: BLE001
            verdict = f"GRADER ERROR: {e}"
        out[name] = verdict.split("—")[0].strip()[:40]
        # The replay scorers carry a REGISTERED_FINGERPRINT of the model file they grade,
        # and an undocumented change to it voids the paired comparison. That has to alert
        # on its OWN channel, at `warn`, independently of the verdict: the dangerous
        # moment is mid-forward-window, when the verdict is still PENDING and the branch
        # below never fires. A fingerprint nobody reads is decoration.
        # Its own message PREFIX, not just its own text: the verdict alert is
        # edge-triggered on the latest row matching `PREREG {name}:%`, so sharing a prefix
        # would let a code alert land between two identical verdicts and re-trigger the
        # verdict — the weekly drumbeat this module was built to avoid.
        if code.get("code_changed_since_registration") and not code.get(
                "change_is_documented"):
            _file_once(conn,
                       f"PREREG-CODE {name}: CODE CHANGED since registration and the"
                       f" change is NOT documented — {str(code.get('note', ''))[:200]}",
                       f"PREREG-CODE {name}:%", level="warn")
        # Third channel, same reasoning as the second and the same prefix discipline: a
        # grader can say "the sample I am accumulating is being pruned by something that
        # is not the hypothesis". That is only actionable WHILE the window is still
        # filling, i.e. exactly while the verdict is PENDING and the branch below is
        # deliberately silent — a pruned forward sample discovered at maturation is a
        # window that has to be thrown away.
        if warn:
            _file_once(conn, f"PREREG-DATA {name}: {str(warn)[:300]}",
                       f"PREREG-DATA {name}:%", level="warn")
        if verdict.startswith("PENDING"):
            continue
        _file_once(conn, f"PREREG {name}: {verdict[:300]}", f"PREREG {name}:%")
    return out


def main():
    from pathlib import Path
    from prediction_market_macro.ingest.store import connect

    db = Path(__file__).resolve().parent.parent / "data" / "macro.db"
    print(json.dumps(run_all(connect(db)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
