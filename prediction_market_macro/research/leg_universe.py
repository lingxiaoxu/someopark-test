"""research/leg_universe.py — the production-identical leg universe, one row per settled
ladder leg x asof offset, for calibration / band studies.

Built ON TOP of `backtest.replay_series(collect_legs=True, collect_leg_meta=True,
params_pit=True)`, so `fair` is exactly what production would have quoted at that asof with
the params then in force (#198). Nothing here re-implements pricing; this module only
flattens what the replay already computes and joins the contract's close_time.

    conda run -n someopark_run python -m prediction_market_macro.research.leg_universe \\
        --out /tmp/leg_universe.csv

Used by docs/BAND_MAP_NOTES.md (2026-09-06). A study that lives only in /tmp is the
"unreproducible frozen row" mistake ops/freeze_track.py was written to end.
"""
from __future__ import annotations

import csv
from datetime import datetime


def build(conn, series: list[str] | None = None,
          offsets: tuple[str, ...] = ("-1h", "-24h")) -> list[dict]:
    from prediction_market_macro.config.registry import REGISTRY
    from prediction_market_macro.research import backtest
    rows: list[dict] = []
    for s in (series or list(REGISTRY)):
        out = backtest.replay_series(conn, s, collect_legs=True, collect_leg_meta=True,
                                     params_pit=True)
        for rec in out["per_release"]:
            for off in offsets:
                legs = rec.get(f"legs{off}") or []
                meta = rec.get(f"legmeta{off}") or []
                for i, (fair, mp, o) in enumerate(legs):
                    m = dict(meta[i]) if i < len(meta) else {}
                    tk = m.get("ticker")
                    cr = (conn.execute("SELECT close_time FROM contracts WHERE ticker=?",
                                       (tk,)).fetchone() if tk else None)
                    rows.append({
                        "series": s, "family": REGISTRY[s].family,
                        "cadence": REGISTRY[s].cadence, "period": rec["period"],
                        "offset": off, "ticker": tk,
                        "close_time": cr["close_time"] if cr else None,
                        "fair": fair, "mp": mp, "out": o,
                        "spread": m.get("spread"), "volume": m.get("volume"),
                        "staleness_s": m.get("staleness_s"),
                        "strike_type": m.get("strike_type"),
                        "floor": m.get("floor"), "cap": m.get("cap"),
                        "model_side": "yes" if fair > mp else "no",
                        "edge_raw": round(fair - mp, 5)})
    return rows


def write_csv(rows: list[dict], path: str) -> int:
    if not rows:
        return 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def main() -> None:
    import argparse
    import sqlite3
    from prediction_market_macro.config.settings import load_settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--series", nargs="*")
    a = ap.parse_args()
    s = load_settings(require_keys=False)
    conn = sqlite3.connect(f"file:{s.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    n = write_csv(build(conn, a.series), a.out)
    print(f"{n} rows -> {a.out}  ({datetime.now().isoformat(timespec='seconds')})")


if __name__ == "__main__":
    main()
