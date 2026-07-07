#!/usr/bin/env python3
"""research/measure_style_distance.py — measure the 48×48 pairwise style distance the paper cites.

For every pair of team adapters (τ, τ′), estimates D(τ,τ′) = E_w TV(π_τ(·|w), π_τ′(·|w)) over a
FIXED probe set of off-ball world-map contexts, by sampling K decisions per (team, probe) from the
SAME serving path the matches use: brain.llm_client.LLM + brain.match_director's OFFBALL_SYS /
OFFBALL_SCHEMA, against the per-team `gemma-<slug>` Ollama models on :11436/:11437.

Outputs research/data/style_distance_matrix.json:
    { teams, matrix (48×48 mean TV), min_nn_distance, argmin_pair, noise_floor, n_probes, k, seed }
The paper's §7 claim then cites: minimum nearest-neighbour TV = X (> 0.3 gate; noise floor Y).

Fair-comparison design: every team answers the IDENTICAL probe list (pooled from data/sft/*.jsonl,
fixed seed), so any residual team hints inside a probe are common input, not a confound. The
plug-in TV from K samples is upward-biased for close distributions; the reported noise_floor
(mean split-half self-TV) is exactly that bias's scale — quote the margin above it.

Resumable: per-team samples cached in research/data/style_cache/<slug>.json — interrupt freely.

Usage (from the repo root; do NOT run while a real sim is using the gemma daemons):

    cd ~/mirofootball && .venv/bin/python research/measure_style_distance.py \\
        --probes 150 --k 20 --urls http://localhost:11436,http://localhost:11437

Cost ≈ teams × probes × k calls (48×150×20 = 144k gemma-2B calls ≈ 4–7 h on the two daemons).
"""
import argparse
import asyncio
import itertools
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]          # ~/mirofootball
sys.path.insert(0, str(ROOT))

from brain.llm_client import LLM                     # noqa: E402 — the match-serving client
from brain.match_director import OFFBALL_SYS, OFFBALL_SCHEMA   # noqa: E402 — the match prompt/schema


def slug(team: str) -> str:
    """'Cote d'Ivoire' → 'gemma-cotedivoire' (matches archived config.json gemma_*_model names)."""
    return "gemma-" + re.sub(r"[^a-z0-9]", "", team.lower())


def extract_user(rec: dict):
    """Pull the user payload out of one SFT jsonl record — OFF-BALL records only.

    The SFT files mix on-ball and off-ball examples; we probe the off-ball policy (that is
    what the per-team adapters serve in matches), so records whose system message is the
    on-ball brain — or whose context says i_have_ball — are skipped."""
    u = None
    if "messages" in rec:
        sys_msg = next((m.get("content", "") for m in rec["messages"] if m.get("role") == "system"), "")
        if "off-ball" not in sys_msg.lower():
            return None
        u = next((m.get("content") for m in rec["messages"] if m.get("role") == "user"), None)
    elif "user" in rec:
        u = rec["user"]
    elif "prompt" in rec:
        u = rec["prompt"]
    if isinstance(u, str):
        try:
            u = json.loads(u)
        except Exception:
            return None
    if not isinstance(u, dict):
        return None
    if (u.get("world") or {}).get("i_have_ball"):
        return None
    return u


def build_probes(sft_dir: Path, n: int, seed: int):
    """Pool user contexts from every team's SFT file, dedupe, fixed-seed sample of n."""
    pool, seen = [], set()
    files = sorted(sft_dir.glob("*.jsonl"))
    if not files:
        sys.exit(f"[style] no SFT files under {sft_dir}")
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 400:            # plenty per team; keeps pooling fast
                    break
                try:
                    u = extract_user(json.loads(line))
                except Exception:
                    continue
                if u is None:
                    continue
                key = json.dumps(u, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    pool.append(u)
    if len(pool) < n:
        print(f"[style] warning: only {len(pool)} unique contexts pooled (< {n}); using all")
        n = len(pool)
    random.Random(seed).shuffle(pool)
    print(f"[style] probe set: {n} contexts (pooled {len(pool)} from {len(files)} SFT files)")
    return pool[:n]


def dkey(d: dict) -> str:
    return f"{d.get('target_zone')}|{d.get('posture')}"


def tv(c1: Counter, n1: int, c2: Counter, n2: int) -> float:
    if not n1 or not n2:
        return float("nan")
    keys = set(c1) | set(c2)
    return 0.5 * sum(abs(c1[k] / n1 - c2[k] / n2) for k in keys)


async def sample_team(team: str, url: str, probes, k: int, cache_dir: Path, sem_n: int):
    """→ {probe_idx: [decision keys]} for one team, cached on disk (resume-friendly)."""
    cf = cache_dir / f"{slug(team)}.json"
    if cf.exists():
        got = json.load(open(cf, encoding="utf-8"))
        if all(len(got.get(str(i), [])) >= k for i in range(len(probes))):
            return {int(i): v for i, v in got.items()}
    llm = LLM(url, slug(team))
    out = {}
    sem = asyncio.Semaphore(sem_n)
    errors = 0

    async def one(i, u):
        nonlocal errors
        async with sem:
            try:
                d = await llm.decide(OFFBALL_SYS, u, OFFBALL_SCHEMA, 64)
                return i, (dkey(d) if isinstance(d, dict) and "_error" not in d else None)
            except Exception:
                errors += 1
                return i, None

    tasks = [one(i, u) for i, u in enumerate(probes) for _ in range(k)]
    for fut in asyncio.as_completed(tasks):
        i, key = await fut
        if key is not None:
            out.setdefault(i, []).append(key)
    json.dump({str(i): v for i, v in out.items()}, open(cf, "w", encoding="utf-8"))
    ok = sum(len(v) for v in out.values())
    print(f"[style] {team:24} {ok}/{len(probes) * k} decisions ({errors} errors) → {cf.name}")
    return out


def pairwise(teams, samples, probes_n):
    """Mean-over-probes TV for every pair + split-half self-TV noise floor."""
    dist = {}
    for a, b in itertools.combinations(range(len(teams)), 2):
        vals = []
        for i in range(probes_n):
            sa, sb = samples[a].get(i, []), samples[b].get(i, [])
            if sa and sb:
                vals.append(tv(Counter(sa), len(sa), Counter(sb), len(sb)))
        dist[(a, b)] = sum(vals) / len(vals) if vals else float("nan")
    floors = []
    for a in range(len(teams)):
        vals = []
        for i in range(probes_n):
            s = samples[a].get(i, [])
            if len(s) >= 4:
                h = len(s) // 2
                vals.append(tv(Counter(s[:h]), h, Counter(s[h:]), len(s) - h))
        if vals:
            floors.append(sum(vals) / len(vals))
    return dist, (sum(floors) / len(floors) if floors else float("nan"))


async def main():
    ap = argparse.ArgumentParser(description="48×48 adapter style-distance measurement")
    ap.add_argument("--probes", type=int, default=150)
    ap.add_argument("--k", type=int, default=20, help="samples per (team, probe)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--urls", default="http://localhost:11436,http://localhost:11437")
    ap.add_argument("--sft-dir", default=str(ROOT / "data" / "sft"))
    ap.add_argument("--lora-dir", default=str(ROOT / "lora"), help="team list = its subdir names")
    ap.add_argument("--concurrency", type=int, default=4, help="in-flight calls per daemon")
    args = ap.parse_args()

    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    teams = sorted(d.name for d in Path(args.lora_dir).iterdir() if d.is_dir())
    print(f"[style] {len(teams)} teams · {args.probes} probes · k={args.k} · "
          f"≈{len(teams) * args.probes * args.k:,} calls across {len(urls)} daemons")

    # verify the per-team ollama models exist before burning hours
    async with httpx.AsyncClient(timeout=10) as h:
        tags = set()
        for u in urls:
            try:
                r = (await h.get(f"{u}/api/tags")).json()
                tags |= {m["name"].split(":")[0] for m in r.get("models", [])}
            except Exception as e:
                sys.exit(f"[style] daemon {u} unreachable: {e}")
    missing = [t for t in teams if slug(t) not in tags]
    if missing:
        sys.exit(f"[style] {len(missing)} team models missing on the daemons, e.g. "
                 f"{[slug(t) for t in missing[:5]]} — check `ollama list`.")

    probes = build_probes(Path(args.sft_dir), args.probes, args.seed)
    cache_dir = ROOT / "research" / "data" / "style_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    samples = []
    for idx, team in enumerate(teams):                 # sequential over teams; parallel within
        url = urls[idx % len(urls)]
        samples.append(await sample_team(team, url, probes, args.k, cache_dir, args.concurrency))

    dist, floor = pairwise(teams, samples, len(probes))
    n = len(teams)
    matrix = [[0.0] * n for _ in range(n)]
    for (a, b), v in dist.items():
        matrix[a][b] = matrix[b][a] = round(v, 4)
    nn = [(min(matrix[a][b] for b in range(n) if b != a), a) for a in range(n)]
    min_d, a_star = min(nn)
    b_star = min((b for b in range(n) if b != a_star), key=lambda b: matrix[a_star][b])

    out = {"teams": teams, "matrix": matrix,
           "min_nn_distance": round(min_d, 4),
           "argmin_pair": [teams[a_star], teams[b_star]],
           "noise_floor_selfTV": round(floor, 4),
           "n_probes": len(probes), "k": args.k, "seed": args.seed,
           "prompt": "brain.match_director OFFBALL_SYS/OFFBALL_SCHEMA (match-identical)"}
    dst = ROOT / "research" / "data" / "style_distance_matrix.json"
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[style] min nearest-neighbour TV = {min_d:.3f} "
          f"({teams[a_star]} ↔ {teams[b_star]}) · self-TV noise floor {floor:.3f}")
    print(f"[style] gate check: min NN {'>' if min_d > 0.3 else '≤'} 0.3 → {dst}")


if __name__ == "__main__":
    asyncio.run(main())
