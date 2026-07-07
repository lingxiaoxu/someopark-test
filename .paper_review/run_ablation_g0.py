#!/usr/bin/env python3
"""research/run_ablation_g0.py — open-loop (g=0) possession ablation runner for measuring β.

Runs engine-only matches (no LLM agents — fast, minutes per run) against an engine started
WITHOUT the anti-dominance feedback, and dumps a per-tick ball log compatible with
research/estimate_beta.py (which reads `ball.team` per line, skipping loose-ball ticks).

SAFETY: never point this at the production engine (:7000). Start a SEPARATE open-loop
engine instance first — the main engine and any running sims are untouched:

    tmux new-session -d -s ablation \\
      'cd ~/mirofootball/engine && MIRO_POSS_G=0 ENGINE_PORT=7001 node server.js >/tmp/engine_ablation.log 2>&1'

Usage (from the repo root, ~/mirofootball):

    .venv/bin/python research/run_ablation_g0.py Brazil Japan 4000 --repeat 5
    .venv/bin/python research/estimate_beta.py research/data/ablation_g0_run*.jsonl --window 100

Then tear down:  tmux kill-session -t ablation
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]          # ~/mirofootball


def load(p):
    return json.load(open(p, encoding="utf-8"))


async def run_match(engine: str, home: str, away: str, n: int, out_path: Path) -> None:
    init = {"team1": load(ROOT / f"data/teams_engine/{home}.json"),
            "team2": load(ROOT / f"data/teams_engine/{away}.json"),
            "pitch": load(ROOT / "engine/init_config/pitch.json")}
    poss = {}
    async with httpx.AsyncClient(timeout=30) as h:
        md = (await h.post(f"{engine}/initiate", json=init)).json()
        hid, aid = md["kickOffTeam"]["teamID"], md["secondTeam"]["teamID"]
        poss = {hid: 0, aid: 0}
        with open(out_path, "w", encoding="utf-8") as f:
            for it in range(n):
                if it == n // 2:
                    md = (await h.post(f"{engine}/secondhalf", json={"matchDetails": md})).json()
                    if "ball" not in md:
                        print(f"[ablation] secondhalf error @ {it}", file=sys.stderr)
                        return
                nm = (await h.post(f"{engine}/iterate", json={"matchDetails": md})).json()
                if "ball" not in nm:
                    print(f"[ablation] iterate error @ {it}", file=sys.stderr)
                    return
                md = nm
                b = md["ball"]
                held = bool(b.get("withPlayer") and b.get("Player"))
                team = b.get("withTeam") if held else None
                if team in poss:
                    poss[team] += 1
                # one line per tick — exactly what estimate_beta.load_holder_series expects
                f.write(json.dumps({"iter": it, "ball": {"team": team}}) + "\n")
    tot = sum(poss.values()) or 1
    ph = poss[hid] / tot
    tag = "EXTREME (monopoly-ish)" if ph >= 0.65 or ph <= 0.35 else "central"
    print(f"[ablation] {home} vs {away} · {n} ticks → holder-ticks {tot}, "
          f"home share {ph:.3f} ({tag}) → {out_path}")


async def main() -> None:
    ap = argparse.ArgumentParser(description="Open-loop (g=0) ablation runner for β measurement")
    ap.add_argument("home"); ap.add_argument("away")
    ap.add_argument("n", type=int, nargs="?", default=4000, help="ticks per match (default 4000)")
    ap.add_argument("--engine", default="http://localhost:7001",
                    help="OPEN-LOOP engine URL (default :7001 — never the production :7000)")
    ap.add_argument("--repeat", type=int, default=1, help="number of matches to run")
    ap.add_argument("--out", default=None,
                    help="output jsonl (default research/data/ablation_g0_run<N>.jsonl)")
    ap.add_argument("--allow-main-engine", action="store_true",
                    help="override the :7000 safety guard (NOT recommended)")
    args = ap.parse_args()

    if ":7000" in args.engine and not args.allow_main_engine:
        sys.exit("[ablation] refusing to run against :7000 (production engine, feedback ON).\n"
                 "Start an open-loop instance:  MIRO_POSS_G=0 ENGINE_PORT=7001 node server.js\n"
                 "then pass --engine http://localhost:7001 (default).")

    data_dir = ROOT / "research" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # continue numbering after any existing runs
    existing = sorted(data_dir.glob("ablation_g0_run*.jsonl"))
    start = len(existing) + 1
    for r in range(args.repeat):
        out = Path(args.out) if (args.out and args.repeat == 1) else \
            data_dir / f"ablation_g0_run{start + r}.jsonl"
        await run_match(args.engine, args.home, args.away, args.n, out)
    print(f"\n[ablation] done. Estimate β over ALL runs:\n"
          f"  .venv/bin/python research/estimate_beta.py "
          f"research/data/ablation_g0_trajectory.jsonl research/data/ablation_g0_run*.jsonl --window 100")


if __name__ == "__main__":
    asyncio.run(main())
