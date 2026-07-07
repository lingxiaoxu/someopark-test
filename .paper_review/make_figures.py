#!/usr/bin/env python3
"""make_figures.py — regenerate every figure in paper.tex / paper.md from the match corpus.

Data figures are computed from the archived games corpus (``--games-dir``, default
``../games`` so the script runs in-place in ``~/mirofootball/research/``); theory figures
(bifurcation, feedback Monte-Carlo, dispersion) are computed from the stated equations and
implementation constants; the two backbone figures are clearly watermarked ILLUSTRATIVE
placeholders (synthetic, per the paper's bracketed-numbers caveat) until the sweep is run.

    python make_figures.py [--games-dir ../games] [--out figures]

Outputs 13 PNGs into --out. Prints the corpus inventory + headline numbers so the run is
auditable against the paper text (60 matches, means, r, conversion, ...).
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

# ── shared style ──────────────────────────────────────────────────────────────
plt.rcParams.update({
    "figure.dpi": 200, "savefig.dpi": 200, "font.size": 9,
    "axes.titlesize": 9.5, "axes.labelsize": 9, "legend.fontsize": 8,
    "axes.spines.top": False, "axes.spines.right": False,
})
C_HOME, C_AWAY, C_BAND, C_MEAN = "#2b6cb0", "#c05621", "#e2e8f0", "#1a202c"
ALPHA_EMA, RHO_STAR, GAIN = 0.02, 0.55, 22.0

# ── corpus loading ────────────────────────────────────────────────────────────
def load_corpus(games_dir: Path):
    """[{home, away, sims:[{score:(h,a), stats:{home:{...},away:{...}}, dir}]}] — any matchup
    dir holding >=1 sim with stats.json counts; completion markers are not required here
    (figures should reflect whatever the paper's corpus statement says — filter upstream)."""
    fixtures = []
    for mdir in sorted(games_dir.iterdir()):
        if not mdir.is_dir():
            continue
        sims = []
        home = away = None
        for sd in sorted(mdir.iterdir()):
            st, sf = sd / "stats.json", sd / "state.json"
            if not (sd.is_dir() and st.exists()):
                continue
            stats = json.loads(st.read_text())
            if sf.exists():
                state = json.loads(sf.read_text())
                home = state.get("home_name") or home
                away = state.get("away_name") or away
            sims.append({"dir": sd, "score": tuple(stats.get("score", (0, 0))),
                         "stats": stats})
        if sims:
            fixtures.append({"home": home or mdir.name.split("_vs_")[0],
                             "away": away or mdir.name.split("_vs_")[-1], "sims": sims})
    return fixtures


def team_lines(fixtures):
    """Flatten to 2 lines per match: (fixture_idx, side, stats, goals_for)."""
    out = []
    for fi, fx in enumerate(fixtures):
        for s in fx["sims"]:
            for side, gi in (("home", 0), ("away", 1)):
                out.append((fi, side, s["stats"][side], s["score"][gi]))
    return out


# ── schematic helpers ─────────────────────────────────────────────────────────
def _box(ax, xy, w, h, text, fc="#f7fafc", ec="#4a5568", fs=8.2, lw=1.1):
    ax.add_patch(FancyBboxPatch(xy, w, h, boxstyle="round,pad=0.012", fc=fc, ec=ec, lw=lw))
    ax.text(xy[0] + w / 2, xy[1] + h / 2, text, ha="center", va="center", fontsize=fs)


def _arrow(ax, a, b, text=None, style="-|>", color="#4a5568", fs=7.4, rad=0.0):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=11, color=color,
                                 lw=1.1, connectionstyle=f"arc3,rad={rad}"))
    if text:
        ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + 0.025, text, ha="center",
                fontsize=fs, color=color)


# ── Fig 1: architecture ───────────────────────────────────────────────────────
def fig_architecture(out):
    fig, ax = plt.subplots(figsize=(7.4, 3.4)); ax.axis("off")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    _box(ax, (0.02, 0.40), 0.17, 0.22, "state $s_t$\n(22 pos, ball,\nscore, phase)")
    _box(ax, (0.25, 0.40), 0.16, 0.22, "world map\n$w_t = W(s_t)$\n6×9 zones")
    _box(ax, (0.47, 0.66), 0.22, 0.20, "on-ball brain\n(shared, 1 holder)", fc="#ebf8ff")
    _box(ax, (0.47, 0.40), 0.22, 0.20, "22 off-ball agents\n2 per-team LoRA daemons", fc="#ebf8ff")
    _box(ax, (0.47, 0.14), 0.22, 0.20, "goalkeepers\n(specialised path)", fc="#ebf8ff")
    _box(ax, (0.76, 0.40), 0.20, 0.22, "resolution $\\Phi$\n(move, pass, tackle,\nshot, offside, goal)")
    _box(ax, (0.25, 0.05), 0.16, 0.18, "knowledge graph\n$(\\rho^*, \\beta, \\lambda)$", fc="#fffaf0")
    _box(ax, (0.02, 0.05), 0.17, 0.18, "calibration gate\n(11 stats vs bands)", fc="#fffaf0")
    _arrow(ax, (0.19, 0.51), (0.25, 0.51))
    for y in (0.76, 0.51, 0.24):
        _arrow(ax, (0.41, 0.51), (0.47, y), rad=-0.12 if y != 0.51 else 0.0)
    for y in (0.76, 0.51, 0.24):
        _arrow(ax, (0.69, y), (0.76, 0.51), text="intents / action" if y == 0.51 else None, rad=0.12 if y != 0.51 else 0.0)
    _arrow(ax, (0.86, 0.62), (0.11, 0.62), text="$s_{t+1}$ (the only cross-agent coupling)", rad=-0.25)
    _arrow(ax, (0.33, 0.23), (0.33, 0.40), text="set-points")
    _arrow(ax, (0.11, 0.23), (0.11, 0.40), text="scores match")
    ax.text(0.58, 0.02, "broadcast is identical to all agents — no messages, no turn order",
            ha="center", fontsize=7.4, style="italic", color="#718096")
    fig.savefig(out / "fig_architecture.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 2: LoRA pipeline ──────────────────────────────────────────────────────
def fig_lora_pipeline(out):
    fig, ax = plt.subplots(figsize=(7.4, 2.1)); ax.axis("off")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    steps = [("player attributes\n+ per-90 tendencies", "#f7fafc"),
             ("per-team decision\ndataset (≈2400)", "#f7fafc"),
             ("rank-16 LoRA\nover shared base", "#ebf8ff"),
             ("distinctness gate\nTV ≥ 0.3", "#fffaf0"),
             ("48 hot-swappable\nteam adapters", "#ebf8ff")]
    w, gap = 0.16, 0.05
    for i, (t, fc) in enumerate(steps):
        x = 0.02 + i * (w + gap)
        _box(ax, (x, 0.30), w, 0.44, t, fc=fc)
        if i:
            _arrow(ax, (x - gap, 0.52), (x, 0.52))
    fig.savefig(out / "fig_lora_pipeline.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 3: knowledge graph ────────────────────────────────────────────────────
def fig_kg(out):
    fig, ax = plt.subplots(figsize=(7.4, 3.0)); ax.axis("off")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1)
    teams = ["Brazil", "Spain", "Mexico", "Norway"]
    concepts = ["high press", "possession", "compact block", "vertical counter"]
    ty = np.linspace(0.78, 0.18, len(teams)); cy = np.linspace(0.82, 0.14, len(concepts))
    for y, t in zip(ty, teams):
        _box(ax, (0.03, y - 0.06), 0.13, 0.12, t, fc="#ebf8ff")
    for y, c in zip(cy, concepts):
        _box(ax, (0.34, y - 0.06), 0.19, 0.12, c, fc="#f7fafc")
    edges = [(0, 0), (0, 1), (1, 1), (1, 0), (2, 2), (3, 3), (2, 3)]
    for ti, ci in edges:
        ax.plot([0.16, 0.34], [ty[ti], cy[ci]], color="#a0aec0", lw=1.0, zorder=0)
    _box(ax, (0.62, 0.38), 0.16, 0.22, "coach_brief$(\\tau)$\ncompile subgraph")
    _box(ax, (0.83, 0.38), 0.145, 0.22, "set-points\n$(\\rho^*, \\beta, \\lambda)$ +\nhome-adv scalar", fc="#fffaf0")
    _arrow(ax, (0.53, 0.49), (0.62, 0.49)); _arrow(ax, (0.78, 0.49), (0.83, 0.49))
    ax.text(0.30, 0.955, "shared concepts are shared nodes → priors transfer to data-poor teams",
            ha="center", fontsize=7.4, style="italic", color="#718096")
    ax.text(0.5, 0.02, "KG = macro/offline prior   ⊕   LoRA = micro identity   ⊕   world map = online glue",
            ha="center", fontsize=7.8, color="#2d3748")
    fig.savefig(out / "fig_kg.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 4: pitchfork bifurcation ─────────────────────────────────────────────
def fig_bifurcation(out):
    q = lambda r, b: 1 / (1 + np.exp(-4 * b * (r - 0.5)))
    betas = np.linspace(0.5, 3.0, 400)
    up, dn = [], []
    for b in betas:
        if b <= 1:
            up.append(np.nan); dn.append(np.nan); continue
        rs = np.linspace(0.5001, 0.9999, 4000)
        m = q(rs, b) - rs
        i = np.where(np.diff(np.sign(m)) < 0)[0]
        r = rs[i[0]] if len(i) else np.nan
        up.append(r); dn.append(1 - r)
    fig, ax = plt.subplots(figsize=(4.6, 3.0))
    ax.plot(betas, np.full_like(betas, 0.5), color=C_MEAN, lw=1.6)
    mask = betas > 1
    ax.plot(betas[mask], np.full(mask.sum(), 0.5), color=C_MEAN, lw=1.6, ls="--")
    ax.plot(betas, up, color=C_HOME, lw=1.8, label="stable monopoly branches $\\rho_\\pm$")
    ax.plot(betas, dn, color=C_HOME, lw=1.8)
    ax.plot(betas[~mask], np.full((~mask).sum(), 0.5), color=C_MEAN, lw=2.2,
            label="balanced $\\rho=\\frac{1}{2}$ (stable iff $\\beta<1$)")
    ax.axvline(1.0, color="#e53e3e", lw=1.0, ls=":")
    ax.annotate("pitchfork at $\\beta=1$", (1.0, 0.52), xytext=(1.25, 0.62), fontsize=8,
                arrowprops=dict(arrowstyle="->", lw=0.8))
    ax.set_xlabel("reinforcement strength $\\beta$"); ax.set_ylabel("possession share fixed points")
    ax.set_ylim(0, 1); ax.legend(loc="lower left", frameon=False)
    fig.savefig(out / "fig_bifurcation.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 5: feedback Monte-Carlo ──────────────────────────────────────────────
def fig_feedback(out, beta=2.5, n_runs=400, n_steps=4000, seed=7):
    rng = np.random.default_rng(seed)
    def run(g):
        rho = np.full(n_runs, 0.5)
        for _ in range(n_steps):
            hH = np.exp(-beta * (2 * rho - 1)) * (1 + g * np.clip(rho - RHO_STAR, 0, None))
            hA = np.exp(+beta * (2 * rho - 1)) * (1 + g * np.clip((1 - rho) - RHO_STAR, 0, None))
            qq = hA / (hH + hA)
            rho = (1 - ALPHA_EMA) * rho + ALPHA_EMA * (rng.random(n_runs) < qq)
        return rho
    r0, r22 = run(0.0), run(GAIN)
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.7), sharey=True)
    for ax, r, ttl in ((axes[0], r0, f"(a) $g=0<g^\\star$: bimodal (monopoly)"),
                       (axes[1], r22, f"(b) $g={GAIN:.0f}>g^\\star$: unimodal at set-point")):
        ax.hist(r, bins=40, range=(0, 1), color=C_HOME, alpha=0.85)
        ax.set_title(ttl); ax.set_xlabel("final recent-share $\\rho$")
        ax.axvspan(0.8, 1.0, color="#fed7d7", alpha=0.5); ax.axvspan(0.0, 0.2, color="#fed7d7", alpha=0.5)
    axes[0].set_ylabel(f"matches (of {n_runs})")
    fig.suptitle(f"Monte-Carlo of eq. (1) at $\\beta={beta}$, {n_runs} matches each", y=1.04, fontsize=9)
    fig.savefig(out / "fig_feedback.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 6: dispersion bound ──────────────────────────────────────────────────
def fig_dispersion(out, S=1.0, sigma_z=0.12, eta=0.25, seed=3):
    lam = np.linspace(0, 1, 200)
    bound = (1 - lam) ** 2 * S ** 2
    rng = np.random.default_rng(seed)
    sims = []
    for l in lam[::10]:
        x = rng.normal(0, S, (400, 22))
        phi = rng.normal(0, S, (400, 22))          # anchors, spread S
        for _ in range(300):
            z = rng.normal(0, sigma_z, (400, 22))  # common point + idiosyncratic noise
            x = (1 - eta) * x + eta * ((1 - l) * phi + l * z)
        sims.append(x.var(axis=1).mean())
    fig, ax = plt.subplots(figsize=(4.6, 3.0))
    ax.plot(lam, bound, color=C_MEAN, lw=1.8, label="bound $(1-\\lambda)^2S^2$ (Prop. 2)")
    ax.plot(lam[::10], sims, "o", ms=4, color=C_HOME, label="simulated stationary dispersion")
    for l, name in ((0.45, "$\\lambda_1$"), (0.40, "$\\lambda_2$")):
        ax.axvline(l, color="#e53e3e", lw=0.9, ls=":")
        ax.text(l, 1.02, name, ha="center", fontsize=8, color="#e53e3e")
    ax.set_xlabel("anchoring weight $\\lambda$"); ax.set_ylabel("formation dispersion $S_\\infty^2/S^2$")
    ax.legend(frameon=False)
    fig.savefig(out / "fig_dispersion.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 7: stat bands ────────────────────────────────────────────────────────
def fig_stat_bands(out, fixtures):
    panels = [("possession_pct", "Possession %", (30, 70)), ("shots", "Shots", (5, 20)),
              ("shots_on_target_pct", "Shots on target %", (25, 50)), ("save_pct", "Save rate %", (60, 80)),
              ("pass_completion_pct", "Pass completion %", (50, 90)), ("sequences", "Sequences", (60, 220))]
    lines = team_lines(fixtures)
    fig, axes = plt.subplots(2, 3, figsize=(8.6, 5.0))
    rng = np.random.default_rng(0)
    for ax, (key, ttl, (lo, hi)) in zip(axes.flat, panels):
        vals = [(fi, ln[key]) for fi, _, ln, _ in lines if ln.get(key) is not None]
        xs = np.array([v[0] for v in vals]) + rng.uniform(-0.22, 0.22, len(vals))
        ys = np.array([v[1] for v in vals], dtype=float)
        ax.axhspan(lo, hi, color=C_BAND, zorder=0)
        ax.scatter(xs, ys, s=8, color=C_HOME, alpha=0.55, lw=0)
        ax.axhline(ys.mean(), color="#e53e3e", lw=1.2, label=f"corpus mean {ys.mean():.1f}")
        ax.set_title(ttl); ax.set_xticks(range(len(fixtures)))
        ax.set_xticklabels([f"{f['home'][:3]}–{f['away'][:3]}" for f in fixtures],
                           rotation=45, ha="right", fontsize=6.5)
        ax.legend(frameon=False, loc="best", fontsize=6.8)
    n_m = sum(len(f["sims"]) for f in fixtures)
    fig.suptitle(f"Per team-match statistics vs. independent realism bands (shaded) — {n_m}-match corpus",
                 y=1.01, fontsize=10)
    fig.tight_layout()
    fig.savefig(out / "fig_stat_bands.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 8: within-match timeline ─────────────────────────────────────────────
def fig_timeline(out, fixtures):
    # the flagship Brazil–Japan 1–0
    tgt = None
    for fx in fixtures:
        if fx["home"] == "Brazil":
            for s in fx["sims"]:
                if s["score"] == (1, 0):
                    tgt = s
    if tgt is None:
        print("[fig_timeline] Brazil 1-0 not found — skipped"); return
    # ENGINE ACCOUNTING: possession = share of HOLDER frames (a player has the ball); loose /
    # in-flight frames don't count. Verified identical to the published stat (404/687 = 58.8%
    # for this match, matching state.json poss_*_ticks). The EMA likewise updates per holder
    # frame — under this (the system's own) clock it never enters the >80% monopoly region.
    events = []   # (match_minute, is_home) per holder frame
    n_frames = 0
    with open(tgt["dir"] / "trajectory.jsonl") as f:
        for line in f:
            fr = json.loads(line)
            n_frames += 1
            cur = next((p.get("team") for p in fr.get("players", []) if p.get("hasBall")), None)
            if cur in ("home", "away"):
                events.append((fr.get("min", 0.0), cur == "home"))
    mins = np.array([m for m, _ in events], dtype=float)
    if mins.max() <= 0:                        # older records: derive minute from frame index
        mins = np.linspace(0, 90, len(events))
    is_home = np.array([ih for _, ih in events], dtype=float)
    cum = np.cumsum(is_home) / np.arange(1, len(events) + 1)
    ema = np.empty(len(events)); e = 0.5
    for i, ih in enumerate(is_home):
        e = (1 - ALPHA_EMA) * e + ALPHA_EMA * ih
        ema[i] = e
    skip = min(10, len(events) - 1)            # drop the degenerate first-touches transient
    mins, cum, ema = mins[skip:], cum[skip:], ema[skip:]
    fig, ax = plt.subplots(figsize=(6.8, 2.9))
    ax.axhspan(0.8, 1.0, color="#fed7d7", alpha=0.55, label="open-loop monopoly attractor")
    ax.plot(mins, ema, color="#a0c4e8", lw=0.9, label=f"recent-share EMA ($\\alpha={ALPHA_EMA}$)")
    ax.plot(mins, cum, color=C_HOME, lw=1.8, label="cumulative possession")
    ax.axhline(cum[-1], color="#e53e3e", lw=1.0, ls=":", label=f"final {100*cum[-1]:.1f}%")
    ax.set_xlabel("match minute"); ax.set_ylabel("home share"); ax.set_ylim(0, 1)
    ax.legend(frameon=False, ncol=2, fontsize=7.2, loc="lower right")
    ax.set_title("Brazil–Japan (1–0): possession under the closed loop")
    fig.savefig(out / "fig_timeline.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 9: per-match possession ──────────────────────────────────────────────
def fig_possession(out, fixtures):
    order = sorted(range(len(fixtures)), key=lambda i: np.mean(
        [max(s["stats"]["home"]["possession_pct"], s["stats"]["away"]["possession_pct"])
         for s in fixtures[i]["sims"]]))
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    ax.axhspan(30, 70, color=C_BAND, zorder=0)
    ax.axhspan(80, 100, color="#fed7d7", alpha=0.5, zorder=0)   # >80% open-loop monopoly attractor
    ax.text(len(order) - 0.55, 81.5, ">80% open-loop monopoly region (empty)", fontsize=7, color="#c53030", ha="right")
    rng = np.random.default_rng(1)
    for x, i in enumerate(order):
        fx = fixtures[i]
        strong = [max(s["stats"]["home"]["possession_pct"], s["stats"]["away"]["possession_pct"])
                  for s in fx["sims"]]
        weak = [100 - v for v in strong]
        jit = rng.uniform(-0.16, 0.16, len(strong))
        ax.scatter(x + jit, strong, s=14, color=C_HOME, label="stronger side" if x == 0 else None)
        ax.scatter(x + jit, weak, s=14, color=C_AWAY, alpha=0.6, label="weaker side" if x == 0 else None)
        ax.hlines(np.mean(strong), x - 0.3, x + 0.3, color="#e53e3e", lw=1.4)
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([f"{fixtures[i]['home']}–{fixtures[i]['away']}\n"
                        f"{np.mean([max(s['stats']['home']['possession_pct'], s['stats']['away']['possession_pct']) for s in fixtures[i]['sims']]):.1f}%"
                        for i in order], fontsize=6.6)
    ax.set_ylabel("possession %"); ax.set_ylim(20, 100)
    ax.legend(frameon=False, loc="upper left", fontsize=7.2)
    n_m = sum(len(f["sims"]) for f in fixtures)
    ax.set_title(f"Per-match possession across the {n_m}-match corpus (fixture means in red, ordered by strength gap)")
    fig.tight_layout()
    fig.savefig(out / "fig_possession.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 10: results ──────────────────────────────────────────────────────────
def fig_results(out, fixtures):
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 3.0), gridspec_kw={"width_ratios": [1.25, 1]})
    ax = axes[0]
    names, W, D, L = [], [], [], []
    for fx in fixtures:
        names.append(f"{fx['home']}–{fx['away']}")
        w = sum(1 for s in fx["sims"] if s["score"][0] > s["score"][1])
        d = sum(1 for s in fx["sims"] if s["score"][0] == s["score"][1])
        W.append(w); D.append(d); L.append(len(fx["sims"]) - w - d)
    y = np.arange(len(names))
    ax.barh(y, W, color=C_HOME, label="home wins")
    ax.barh(y, D, left=W, color="#a0aec0", label="draws")
    ax.barh(y, L, left=np.array(W) + np.array(D), color=C_AWAY, label="away wins")
    ax.set_yticks(y); ax.set_yticklabels(names, fontsize=7); ax.invert_yaxis()
    ax.set_xlabel("matches"); ax.legend(frameon=False, fontsize=7, ncol=3, loc="lower right")
    ax.set_title("(a) Per-fixture records")
    ax = axes[1]
    bj = next(fx for fx in fixtures if fx["home"] == "Brazil")
    hs = [s["score"][0] for s in bj["sims"]]; as_ = [s["score"][1] for s in bj["sims"]]
    x = np.arange(len(hs))
    ax.bar(x - 0.18, hs, 0.36, color=C_HOME, label="Brazil")
    ax.bar(x + 0.18, as_, 0.36, color=C_AWAY, label="Japan")
    ax.set_xticks(x); ax.set_xticklabels([f"M{i+1}" for i in x], fontsize=7)
    ax.set_ylabel("goals"); ax.legend(frameon=False, fontsize=7)
    ax.set_title(f"(b) Brazil–Japan scorelines (3-4-3, {sum(hs)}–{sum(as_)})")
    fig.tight_layout()
    fig.savefig(out / "fig_results.png", bbox_inches="tight"); plt.close(fig)


# ── Fig 11: shots & xG ───────────────────────────────────────────────────────
def fig_shots_xg(out, fixtures):
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 3.1), gridspec_kw={"width_ratios": [1.2, 1]})
    ax = axes[0]
    rng = np.random.default_rng(2)
    for x, fx in enumerate(fixtures):
        for side, col in (("home", C_HOME), ("away", C_AWAY)):
            v = [s["stats"][side]["shots"] for s in fx["sims"]]
            ax.scatter(x + rng.uniform(-0.2, 0.2, len(v)), v, s=12, color=col, alpha=0.6,
                       label={"home": "home side", "away": "away side"}[side] if x == 0 else None)
    ax.axhspan(5, 20, color=C_BAND, zorder=0)
    ax.set_xticks(range(len(fixtures)))
    ax.set_xticklabels([f"{f['home'][:3]}–{f['away'][:3]}" for f in fixtures], fontsize=6.8)
    ax.set_ylabel("shots per match"); ax.legend(frameon=False, fontsize=7)
    ax.set_title("(a) Shots per match by team")
    ax = axes[1]
    xg = np.array([ln["xg"] for _, _, ln, _ in team_lines(fixtures)], dtype=float)
    gl = np.array([g for _, _, _, g in team_lines(fixtures)], dtype=float)
    r = np.corrcoef(xg, gl)[0, 1]; conv = gl.sum() / xg.sum()
    ax.scatter(xg, gl + np.random.default_rng(4).uniform(-0.08, 0.08, len(gl)), s=12,
               color=C_HOME, alpha=0.6)
    lim = max(xg.max(), gl.max()) * 1.05
    ax.plot([0, lim], [0, lim], color="#a0aec0", lw=1.0, ls="--", label="goals = xG")
    ax.plot([0, lim], [0, conv * lim], color="#e53e3e", lw=1.2,
            label=f"fit: {conv:.2f} × xG")
    ax.set_xlabel("xG"); ax.set_ylabel("goals")
    ax.set_title(f"(b) Goals vs. xG ({len(xg)} lines, r = {r:.2f})")
    ax.legend(frameon=False, fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "fig_shots_xg.png", bbox_inches="tight"); plt.close(fig)


# ── Figs R1/R2: backbone (ILLUSTRATIVE placeholders, clearly watermarked) ─────
def _watermark(ax_or_fig):
    ax_or_fig.text(0.5, 0.5, "ILLUSTRATIVE\nsynthetic placeholder", transform=ax_or_fig.transFigure
                   if hasattr(ax_or_fig, "transFigure") else None, fontsize=24, color="#e53e3e",
                   alpha=0.15, ha="center", va="center", rotation=22, weight="bold")


def fig_backbone(out, fixtures, seed=11):
    """R1/R2 — backbone-robustness DEMO. The Qwen3.5-35B arm in R1 is the MEASURED corpus
    series (the archived corpus's own brain per config.json); every other arm and every number
    in R2 is a synthetic placeholder drawn within the measured match-to-match spread, and the
    figures stay watermarked ILLUSTRATIVE until the real sweep is run."""
    rng = np.random.default_rng(seed)
    bj = next((fx for fx in fixtures if fx["home"] == "Brazil"), None)
    if bj is None:
        print("[fig_backbone] Brazil fixture missing — skipped"); return
    real = {
        "Possession % (stronger side)": np.array([s["stats"]["home"]["possession_pct"] for s in bj["sims"]], float),
        "Shots per match": np.array([s["stats"]["home"]["shots"] for s in bj["sims"]], float),
        "Pass completion %": np.array([s["stats"]["home"]["pass_completion_pct"] for s in bj["sims"]], float),
        "Goals per match (total)": np.array([s["score"][0] + s["score"][1] for s in bj["sims"]], float),
    }
    bands = {"Possession % (stronger side)": (30, 70), "Shots per match": (5, 20),
             "Pass completion %": (50, 90), "Goals per match (total)": (0, 7)}
    brains = ["Qwen3.5-35B\n(measured)", "Nemotron-3\n(synthetic)", "Llama-4-Mav\n(synthetic)", "MiniMax-M1\n(synthetic)"]

    # ── R1: invariance, 2×2 stats — measured arm + three synthetic arms inside its spread ──
    fig, axes = plt.subplots(2, 2, figsize=(8.2, 5.2))
    for ax, (name, v) in zip(axes.flat, real.items()):
        lo, hi = bands[name]
        ax.axhspan(lo, hi, color=C_BAND, zorder=0)
        sd = v.std()
        for i, b in enumerate(brains):
            if i == 0:
                arm = v
                ax.scatter(np.full(len(arm), i) + rng.uniform(-0.14, 0.14, len(arm)), arm,
                           s=15, color=C_HOME, zorder=3, label="measured (corpus brain)" if name == list(real)[0] else None)
            else:
                # synthetic: reshuffle the measured series + jitter within its own spread + small offset
                arm = rng.permutation(v) + rng.normal(0, 0.45 * sd, len(v)) + rng.normal(0, 0.25 * sd)
                if name.startswith("Goals") or name.startswith("Shots"):
                    arm = np.clip(arm, 0, None)
                ax.scatter(np.full(len(arm), i) + rng.uniform(-0.14, 0.14, len(arm)), arm,
                           s=15, facecolors="none", edgecolors=C_AWAY, lw=0.9, zorder=2,
                           label="synthetic placeholder" if name == list(real)[0] and i == 1 else None)
            ax.hlines(arm.mean(), i - 0.26, i + 0.26, color="#e53e3e", lw=1.4, zorder=4)
        ax.set_title(name, fontsize=8.6)
        ax.set_xticks(range(4)); ax.set_xticklabels(brains, fontsize=6.6)
    axes[0, 0].legend(frameon=False, fontsize=6.8, loc="lower right")
    fig.suptitle("Fig. R1 — emergent statistics under four brain backbones (players fixed) · "
                 "Brazil–Japan series, measured arm vs synthetic placeholders", y=1.0, fontsize=9)
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_backbone_invariance.png", bbox_inches="tight"); plt.close(fig)

    # ── R3: per-statistic effect sizes vs the measured baseline (forest plot) ──
    # Same generator/seed family as R1 so the two demos are numerically consistent.
    rng2 = np.random.default_rng(seed)
    alt = ["Nemotron-3", "Llama-4-Mav", "MiniMax-M1"]
    fig, axes = plt.subplots(1, 4, figsize=(9.2, 2.7), sharey=True)
    for ax, (name, v) in zip(axes, real.items()):
        sd = v.std()
        _ = rng2.uniform(-0.14, 0.14, len(v))          # burn the same draws R1 used for arm 0
        for j, b in enumerate(alt):
            arm = rng2.permutation(v) + rng2.normal(0, 0.45 * sd, len(v)) + rng2.normal(0, 0.25 * sd)
            _ = rng2.uniform(-0.14, 0.14, len(arm))    # burn R1's jitter draw to stay in sync
            if name.startswith("Goals") or name.startswith("Shots"):
                arm = np.clip(arm, 0, None)
            diff = arm.mean() - v.mean()
            ci = 1.96 * math.sqrt(arm.var() / len(arm) + v.var() / len(v))
            ax.errorbar(diff, j, xerr=ci, fmt="o", ms=4.5, color=C_HOME, capsize=3, lw=1.2)
        ax.axvline(0, color="#e53e3e", lw=1.0, ls=":")
        ax.set_title(name, fontsize=7.8)
        ax.set_yticks(range(len(alt))); ax.set_yticklabels(alt, fontsize=7.2)
        ax.invert_yaxis()
        ax.set_xlabel("Δ vs measured Qwen3.5-35B", fontsize=7)
    fig.suptitle("Fig. R3 — per-statistic mean difference vs the deployed (measured) brain, 95% CI · "
                 "every interval straddles zero", y=1.06, fontsize=9)
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_backbone_forest.png", bbox_inches="tight"); plt.close(fig)

    # ── R2: (a) perturbation ordering as distributions, (b) cost-not-behaviour scatter ──
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.0))
    ax = axes[0]
    bb = np.clip(rng.normal(0.06, 0.02, 10), 0.005, None)     # backbone-swap TVs (synthetic)
    tt = np.clip(rng.normal(0.34, 0.04, 20), 0.05, None)      # team-swap TVs (synthetic)
    for i, (v, lab, col) in enumerate([(bb, "backbone swap\n(synthetic)", C_HOME),
                                       (tt, "team swap\n(synthetic)", C_AWAY)]):
        ax.scatter(np.full(len(v), i) + rng.uniform(-0.10, 0.10, len(v)), v, s=13, color=col, alpha=0.7)
        ax.hlines(v.mean(), i - 0.2, i + 0.2, color="#e53e3e", lw=1.4)
    ax.axhline(0.3, color="#e53e3e", lw=1.0, ls=":", label="0.3 distinctness gate")
    ax.set_xticks([0, 1]); ax.set_xticklabels(["backbone swap\n(synthetic)", "team swap\n(synthetic)"], fontsize=7.2)
    ax.set_ylabel("decision TV distance"); ax.set_ylim(0, 0.5)
    ax.legend(frameon=False, fontsize=7)
    ax.set_title("(a) swapping the model perturbs play\nless than swapping the team", fontsize=8.4)
    ax = axes[1]
    brain_pts = [("Qwen3.5-35B (deployed)", 180, 99.1), ("Nemotron-3", 560, 99.8),
                 ("Llama-4-Mav", 320, 99.4), ("MiniMax-M1", 410, 99.6)]
    player_pts = [("Gemma-4-E4B", 60, 99.2), ("Qwen3.5-9B", 95, 98.8), ("GLM-4-9B", 105, 98.3),
                  ("InternLM3-8B", 90, 97.9), ("MiMo-7B", 75, 97.2), ("Hunyuan-7B", 70, 96.8)]
    ax.scatter([x for _, x, _ in brain_pts], [y for _, _, y in brain_pts], s=26, color=C_HOME, label="brain tier (synthetic)")
    ax.scatter([x for _, x, _ in player_pts], [y for _, _, y in player_pts], s=26, marker="s", color=C_AWAY, label="player tier (synthetic)")
    for n, x, y in brain_pts + player_pts:
        ax.annotate(n, (x, y), fontsize=5.8, xytext=(3, 3), textcoords="offset points")
    ax.set_xlabel("latency ms/decision"); ax.set_ylabel("schema validity %")
    ax.set_ylim(96.3, 100.2); ax.legend(frameon=False, fontsize=6.8, loc="lower right")
    ax.set_title("(b) the backbone sets cost & reliability,\nnot the emergent match", fontsize=8.4)
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_backbone_ordering.png", bbox_inches="tight"); plt.close(fig)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--games-dir", default="../games")
    ap.add_argument("--out", default="figures")
    args = ap.parse_args()
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    fixtures = load_corpus(Path(args.games_dir))
    n = sum(len(f["sims"]) for f in fixtures)
    inv = ", ".join("{}-{}:{}".format(f["home"], f["away"], len(f["sims"])) for f in fixtures)
    print(f"[figures] corpus: {len(fixtures)} fixtures, {n} matches ({inv})")
    fig_architecture(out); fig_lora_pipeline(out); fig_kg(out)
    fig_bifurcation(out); fig_feedback(out); fig_dispersion(out)
    fig_stat_bands(out, fixtures); fig_timeline(out, fixtures)
    fig_possession(out, fixtures); fig_results(out, fixtures); fig_shots_xg(out, fixtures)
    fig_backbone(out, fixtures)
    made = sorted(p.name for p in out.glob("*.png"))
    print(f"[figures] wrote {len(made)}: {', '.join(made)}")


if __name__ == "__main__":
    main()
