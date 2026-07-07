# someopark-football-agentic-simulator: A Multi-Agent LLM System for Football Simulation with Low-Rank Team Identity and Stabilised Emergent Possession

**Lingxiao Xu** — Someo Park — `admin@someopark.com`

> The system described here was designed and implemented in full by the author; code and the match
> corpus reproduce every figure and number. This Markdown is a readable companion to `paper.pdf`
> (built from `paper.tex`); the Chinese version is `paper_zh.md` / `paper_zh.pdf`.

---

## Abstract

We present **someopark-football-agentic-simulator (SFAS)**, a multi-agent system in which twenty-two
LLM agents play a full ninety-minute football match. The design is deliberately minimal: every frame,
the system broadcasts one shared, coarsened **world map** to all agents; each agent, queried in
parallel and in isolation, returns an intent or a ball action; and a single deterministic
**resolution operator** advances the physical state. The agents never exchange messages. Team
identity is not a prompt but a **parameter** — each of forty-eight national teams is a rank-16 LoRA
over one shared base policy, distilled from player attributes and statistical tendencies and
hot-swapped at serve time — and pre-match tactics are grounded in a **knowledge graph** that supplies
a handful of control set-points. This is a *systems* contribution: we describe the architecture, the
per-team adaptation pipeline, and the knowledge-graph grounding, and we explain **why the system is
stable**. Two emergent failure modes that sink the naive design — possession *snowballing* to a
monopoly, and formations *collapsing* onto the ball — are generic dynamical phenomena, each cured by
a control law with a sharp closed-form threshold: a recent-share feedback that removes a pitchfork
bifurcation once its gain exceeds **g\* = 4(β−1)**, and a convex intent-anchoring blend that
guarantees a positive lower bound **(1−λ)²S²** on formation spread. On a 75-match corpus over seven
simulated 2026 World-Cup fixtures — from evenly matched pairs to heavy mismatches — the standard
statistics land in independent realism bands (pass completion in-band in all 150 team-matches); a
six-match open-loop ablation reproduces the predicted bimodal possession monopoly; play is
competitive rather than scripted (the clearly weaker side still wins 14% of matches — e.g. Paraguay
beating France 1–0 while conceding 69.5% possession); and the dominant side's possession is
venue-symmetric (68.2% at home vs 68.6% away across the two most mismatched fixtures).

**Keywords:** multi-agent systems; LLM agents; agentic simulation; emergent coordination; low-rank
adaptation; knowledge graphs; decentralised control; sports simulation.

---

## 1. Introduction

LLMs are increasingly deployed as **agents** (perceive → decide → act). Most work studies one agent,
or a few agents that *talk*. We study **many** LLM agents sharing one fast, adversarial, physically
grounded world, and the questions that regime raises: **(i)** how do agents that cannot communicate
coordinate, and **(ii)** when the collective behaviour is an emergent, self-reinforcing quantity,
what keeps it realistic instead of running away to a degenerate state?

A football match is the ideal instance: closed, twenty-two agents, a hard physical substrate (the
ball is indivisible and conserved), rich heterogeneity, team identity, and **measurable** aggregate
invariants. Unlike RL football environments that train low-level motor policies from reward, SFAS
treats each player as a **language-model agent** reasoning over a symbolic view of the pitch; the
appeal is interpretability and zero-shot tactical control, and the challenge is that nothing in the
LLM stack guarantees the emergent match will be realistic. Making it realistic, and understanding
*why*, is the content of this paper.

**The system (Fig. 1).** Each frame SFAS broadcasts one **world map** wₜ — a coarsened common
description of all 22 positions (a 6×9 zone grid), ball, score and phase — to every agent. Off the
ball each agent returns an **intent** (target zone + posture); on the ball, the holder returns a
categorical ball action. Every agent acts on **every** frame, simultaneously and in isolation —
queried **in parallel, no message channel, no turn order**. A
deterministic **resolution operator** Φ then advances the state (movement, passes, tackles, shots,
offside, goals). Team identity is a rank-16 LoRA over one shared base; tactics come from a knowledge
graph; an outer calibration gate checks 11 statistics against real-football bands. ≈4000 frames
produce a complete match.

![Architecture](figures/fig_architecture.png)
**Figure 1.** The SFAS agentic loop. A shared world map is broadcast to 22 isolated agents (one
shared on-ball "brain" policy and two per-team off-ball LoRA policies); their intents are merged only
by the deterministic operator Φ. A knowledge graph supplies set-points; a calibration gate scores the
match. The recursion through Φ is the *only* cross-agent coupling.

**Why a naive build fails.** (1) Possession **snowballs**: an early ball-winner keeps it all match
(80–90%) because holding is self-reinforcing. (2) Followed literally, every agent's intent is "go to
the ball," so the formation **collapses** to a point. Both are *dynamical*, not implementational, and
each is cured by a control law with a sharp threshold.

**Contributions.** A *system* (SFAS: shared-environment coordination, LoRA team identity, KG tactics)
formalised as a **stigmergic Markov game** (Prop. 1). *Why it is stable*: possession undergoes a
pitchfork at β=1 (Thm. 1), removed by feedback gain **g\* = 4(β−1)** (Thm. 2) with a home-advantage
set-point (Cor.); intent anchoring guarantees dispersion ≥ (1−λ)²S² (Prop. 2). *Validation* on a
simulated World-Cup corpus (§7).

## 2. Related work

**LLM agents / societies:** Generative Agents (Park et al. 2023), Voyager (Wang et al. 2023), agent
surveys (Wang et al. 2023); many coordinate by *communication* (CAMEL, Li et al. 2023) and act via
ReAct loops (Yao et al. 2023). SFAS's agents never communicate. **Multi-agent learning / team
games:** AlphaStar (Vinyals et al. 2019), OpenAI Five (Berner et al. 2019), hide-and-seek (Baker et
al. 2020), RoboCup (Kitano et al. 1997), Google Research Football (Kurach et al. 2020) — these
*optimise* policies from reward; we *prescribe* LLM agents and *analyse* emergent invariants.
**Decentralised control / stigmergy / mean field:** Dec-POMDP (Bernstein et al. 2002), stigmergy
(Theraulaz & Bonabeau 1999), mean-field games / MARL (Lasry & Lions 2007; Yang et al. 2018).
**Methods:** LoRA (Hu et al. 2021), QLoRA (Dettmers et al. 2023), RAG / GraphRAG (Lewis et al. 2020;
Edge et al. 2024), football scoring (Maher 1982; Dixon & Coles 1997; Skellam 1946), reinforced
processes (Pemantle 2007), stochastic approximation (Benaïm 1999; Kushner & Yin 2003), pitchfork
(Strogatz 1994).

## 3. System architecture

SFAS is a **real-time** simulation advanced in discrete **frames**, exactly as a game engine renders
a match frame by frame: a frame is an instantaneous sample of continuously evolving play, not a
player's "turn." All 22 agents perceive and act on **every** frame, simultaneously and independently
— no turn order, no alternation. A frame advances physical time by a small fixed increment, and the
≈4000-frame match is built from these fixed simulation time-steps — the way a game engine advances its
world each frame — not from rounds. The subscript `t` indexes frames in this sense.

**State / world map / resolution operator.** State `sₜ = ({xᵢ,rᵢ}, bₜ, cₜ, phₜ)`. Agents never see
`sₜ`; they see the shared **world map** `wₜ = W(sₜ)` (6×9 zone grid + roles + ball flag + score +
phase), broadcast identically to all. The deterministic **resolution operator** `Φ` moves each player
a bounded step toward a blend of its intent and its formation anchor, resolves one ball action by a
skill-weighted contest, enforces one-holder conservation, and adjudicates tackles/passes/shots/
offside/goals. `Φ` carries the environment's randomness (skill contests) and is the only place the 22 actions interact; the agent policies are themselves stochastic.

**Three policies and serving.** (a) A shared **on-ball "brain"** decides the single holder's action
(larger model, re-queried every few frames as a latency optimisation — compute throttling, not
turn-taking; the holder still acts every frame, its last action persisting between queries). (b) Two **off-ball** policies (one per team) decide every
other player's intent — the team LoRAs of §4, served as two independent daemons. (c) Goalkeepers use
a specialised path. Because agents don't communicate (Prop. 1), all 22 queries run in parallel and
the two team daemons run concurrently — this is what makes a 4000-frame match tractable locally.

**Model-agnostic backbones.** The policies reach their models through one narrow interface (structured
prompt in, schema-constrained decision out), so the architecture is tied to no model family. We run the
on-ball brain as **Nemotron-3 Super (120B)** and, on a second machine, as **Qwen3.5-35B-A3B**, and the
players as **Gemma-4-E2B** adapters; the emergent invariants (§6) are qualitatively unchanged under
either brain — they are properties of the control laws, not the backbone. Any instruction-tuned model
that emits the decision schema drops in: comparable backbones include **Llama-4-Maverick, Qwen3.5-72B,
Mistral-Large-2, DeepSeek-V3.2, MiniMax-M1** (brain tier) and **Gemma-4-E4B, Qwen3.5-9B, GLM-4-9B
(Zhipu), InternLM3-8B, MiMo-7B (Xiaomi), Hunyuan-7B (Tencent)** (3–10B player tier). Since identity is
in the rank-16 adapters and tactics in the KG (not the
base weights), swapping the backbone changes neither the team library nor the control laws.

## 4. Encoding team identity with per-team LoRA

**Team identity is a parameter, not a prompt** (Fig. 2). We *train* a small adapter that shifts the
base policy toward a team's characteristic choices.

![LoRA pipeline](figures/fig_lora_pipeline.png)
**Figure 2.** Player attributes + per-90 tendencies → a per-team decision dataset (≈2400 examples) →
a rank-16 LoRA over one shared base → a per-team policy, admitted only if it clears a distinctness
gate. 48 teams = 48 hot-swappable adapters.

- **Decision dataset.** For each team, ≈2400 (world-map context, decision) pairs generated from
  player attributes (finishing, passing, tackling, pace, role) + per-90 tendencies.
- **Low-rank adaptation.** Team τ is `W₀ + B_τ A_τ`, rank r = 16 ≪ d, SFT over a frozen base. *Cost*:
  tens of MB each → all 48 teams fit beside one base, hot-swapped at serve time. *Parsimony*: identity
  is a perturbation of a shared football prior → regularised and low-dimensional.
- **Distinctness gate.** Admitted only if its decision distribution differs from every other team's by
  ≥ 0.3 total variation (§8.1), guaranteeing 48 genuinely different styles.

## 5. Knowledge-graph tactical grounding

Per-team LoRA encodes *who the players are*; it does not encode *how the team intends to play as a
unit*, nor let a designer edit tactics. SFAS adds a **tactical knowledge graph** — and, crucially, the
KG is the component that makes LoRA and the message-free framework work better **together** (Fig. 3).

![Knowledge graph synergy](figures/fig_kg.png)
**Figure 3.** (1) A relational graph links the 48 teams to shared tactical *concepts* (high press,
possession, compact block, vertical counter); shared styles are shared nodes, so knowledge
**transfers** and data-poor teams inherit priors. `coach_brief(τ)` compiles the subgraph into
set-points `(ρ*, β, λ)` + a home-advantage scalar. (2) These set-points are what the control laws
enforce, what the LoRA's micro-decisions operate within, and what aligns the message-free agents.
**KG = macro/offline prior ⊕ LoRA = micro identity ⊕ world map = online glue.**

**What it is / how it works.** A relational ontology over all 48 teams: nodes are teams, tactical
concepts, roles, combinations; a concept shared by several teams is *one* node, so the graph is
densely interconnected. A lightweight deterministic compilation builds it; an optional LLM pass
enriches it with narrative text indexed for hybrid retrieval (RAG / GraphRAG). At match time
`coach_brief(τ)` retrieves τ's subgraph and compiles it into the control set-points the rest of the
system consumes (possession target → the *fixture* anchor of §6.2, compiled from **both** teams'
style vectors by the learned calibration policy; pressing → β; tightness → λ). The
read is offline and self-contained — no live database in the match loop.

**How it improves the system.** (i) *Consistency* — the same tactical facts every match. (ii)
*Transfer* — shared styles are shared nodes, so a data-poor team inherits priors from its neighbours.
(iii) *An interpretable, editable interface* — edit one node, the change propagates into set-points.
(iv) *Separation of concerns* — KG = team-level intent, LoRA = player-level tendency.

**Synergy with LoRA and the agent framework.**
- **Graph ⊕ LoRA: macro intent meets micro identity.** LoRA shifts a player's *micro* decision
  distribution; the KG sets the *macro* envelope the team plays within. A possession set-point with a
  route-one striker's adapter gives patient build-up ending in early shots — a combination no single
  prompt reliably elicits. The KG also *compensates for the LoRA's data limits*: a thin adapter still
  gets a sensible tactical prior via shared-concept edges. The KG picks ρ*, β, λ; the LoRA picks the
  per-frame action distribution; the calibration gate checks the joint result.
- **Graph ⊕ agent framework: the team-level correlation device.** Agents are message-free (Prop. 1):
  within a frame they coordinate only through the world map, which carries *positional* but not
  *strategic* common knowledge — there is no in-match channel for 11 teammates to agree to press high.
  The KG fills exactly this gap: an **offline, team-level correlation device** (a manager's pre-match
  instruction internalised via shared set-points), where the world map is the **online, frame-level**
  device. Different time-scales, complementary. The graph sets the *set-points*, the world map carries
  the *state*, the LoRA supplies the *policy*, the control laws guarantee stability.

## 6. Why the system is stable

(Proofs in Appendix A; here, mechanism + figures.)

### 6.1 Coordination without communication

A **stigmergic Markov game**: every agent observes the same `wₜ = W(sₜ)`, has no other input (no
communication, no peek at others within a frame), state advances by `sₜ₊₁ = Φ(sₜ, **a**ₜ)`.

> **Proposition 1 (factorisation).** `P(**a**ₜ | sₜ) = ∏ᵢ πᵢ(aᵢ | W(sₜ))`. All cross-agent dependence
> is generated by Φ and the recursion; none within a decision step. The shared map is a correlation
> device.

This licenses the parallel, message-free serving of §3, and tells us coordination failures live in
the interaction of the product policy with Φ over time.

### 6.2 Possession is bistable — and a feedback law fixes it

Home's recent share is tracked by an EMA `ρₜ₊₁ = (1−α)ρₜ + α βₜ₊₁` (eq. 1). Reinforced turnover
hazards `h_H = h₀e^{−β(2ρ−1)}`, `h_A = h₀e^{+β(2ρ−1)}` give next-holder probability `q(ρ) =
σ(4β(ρ−½))`. Eq. (1) is a constant-gain stochastic approximation.

> **Lemma 1 (mean-field reduction).** Eq. (1) is a Robbins–Monro recursion whose mean field is the
> gradient flow `ρ̇ = m(ρ) = q(ρ) − ρ`. By the ODE method (Benaïm 1999) the trajectory converges to
> the stable fixed points of m, avoids unstable ones a.s., and fluctuates O(√α) around a stable point.

> **Theorem 1 (bistability).** ρ = ½ is stable iff β < 1. At β = 1 a *supercritical pitchfork*: for
> β > 1, ½ is unstable and two stable monopoly fixed points ρ± appear, → {0,1} as β → ∞. So for any
> β > 1 the uncontrolled simulator drives possession to a near-monopoly a.s. (Fig. 4).

![Bifurcation](figures/fig_bifurcation.png)
**Figure 4.** The open-loop possession mean field undergoes a supercritical pitchfork at β = 1: the
balanced 50% equilibrium loses stability and two possession-monopoly branches emerge (Thm. 1).

The cure: once a team's recent share passes a deadband ρ*, inflate *its* turnover hazard by
`(1 + g(ρ−ρ*)₊)`, gain g.

> **Theorem 2 (stabilisation, critical gain).** With β > 1, ρ* = ½: the balanced state is stable iff
> **g > g\* = 4(β−1)**. Then it is the *unique* stable equilibrium, the monopolies are destroyed, and
> possession concentrates in an O(√α) neighbourhood of the target.

Make the anti-dominance gain larger than 4× the excess reinforcement, and a guaranteed monopoly
becomes a controlled match. The system uses **g = 22, α = 0.02 (≈50-frame window), ρ* = 0.55**; g = 22
clears g\* for every β up to 6.5. Figure 5 confirms it by Monte-Carlo: bimodal (g = 0) → unimodal
(g = 22).

![Feedback Monte-Carlo](figures/fig_feedback.png)
**Figure 5.** Monte-Carlo of eq. (1) at β = 2.5 (400 matches each). (a) g = 0 < g\*: bimodal —
possession monopoly. (b) g = 22 > g\*: unimodal at the set-point, as Theorem 2 predicts.

> **Corollary (home-advantage set-point).** A small home edge moves the unique fixed point to
> `ρ† = ½ + (η_H+η_A)/(2(g−g\*)) + O(η²)` — one scalar shifts the calibrated possession target
> continuously and monotonically. *The graph sets the set-point; the control law enforces it.*

**Matchup-dependent calibration, learned rather than hand-set.** The corollary's set-point is not a
global constant: it is compiled *per fixture* from both teams' real style vectors (a 48-team library
of measured possession, pass-completion and strength statistics). The anchor is
`ρ†(h,a) = clip(½ + κ·[w_p·Δposs + w_c·Δpass + w_z·Δz] + η_home, ½ ± b)` with band b = 0.065, so
Spain–Austria and Brazil–Japan get *different* calibrations while no two fixtures' anchors can
diverge by more than 13 points. Enforcement is deliberately soft — *guided emergence*: the anchor
sets a low-gain director target (k_p = 0.4, a coach's nudge rather than a pull) and a weakened
physical lean, while three style channels enter as **behavioural** modifiers — pressing scales the
opponent's turnover hazard by (1 + 0.6·(press−½)) and tightens marking distances by
(1 − 0.30·(press−½)) inside the contextual pass-completion model; tempo scales the holder's release
hazard; directness rescores pass-target selection — so the possession difference *emerges from the
behavioural contest* rather than being imposed. The eight calibration parameters
θ = (κ, w_p, w_c, w_z, lean scale, three style gains) are **learned** by cross-entropy-method (CEM)
policy search over engine rollouts, with per-fixture loss |emergent share − real-world anchor| +
a monopoly penalty + a pass-completion band penalty, trained on three strength-diverse fixtures;
the loss falls 0.232 → 0.150 in four iterations (population 8), giving κ = 1.18, w_p = 0.44,
w_c = 0.11, w_z = 0.01 and press/directness/tempo gains 0.69/0.38/0.50 (policy archived in
`calib_policy.json`; trainer `calibrate_policy.py`). The two §7 observables — the dominant side's
seat scaling smoothly with the strength gap, and its venue symmetry (68.2% home vs 68.6% away) —
are direct consequences of this fixture-dependent, learned anchoring.

**Remark (deadband, operating point, inferring β).** Theorem 2 analyses ρ\* = ½, where the threshold
is exactly g\* = 4(β−1). The deployed ρ\* = 0.55 leaves the feedback inactive near ½ (so ½ is locally
unstable, open-loop slope β−1 > 0); the share drifts to the band edge, where feedback engages — which
is why balanced fixtures seat possession just past the band edge (55–60%) even before any home edge,
with the matchup-dependent set-point then trimming it to the KG target. **β is measured from six open-loop (g = 0) ablation matches** (4,325
held ticks; 36 window-transition pairs at W = 100). The primary evidence of supercriticality is
behavioural (Fig. 6): five of the six matches drift to the extremes — 69% of share windows fall at
ρ ≤ 0.35 or ρ ≥ 0.65 and only 5% remain central, with leading modal share ρ⁺ = 0.68 — whereas the
closed loop on balanced fixtures is unimodal (11% extreme, 33% central). By Theorem 1 read in
reverse, such drift occurs only for β > 1.
Point estimates agree in sign: the local slope of the share-transition map at ρ\* = ½ (the direct
estimator) gives β̂ ≈ 1.6–2.3, and the modal fixed-point relation β = logit(ρ⁺)/(4(ρ⁺−½)) gives
β̂ ≈ 1.05; a *global* logistic fit yields 0.77–0.91, an underestimate by construction here since it
pools the saturated branches where the drift is clipped (the same mechanism that makes the AR(1)
slope a lower bound, ≈ 0.7–0.8). A synthetic recovery study (same estimator pipeline on eq. (1)
trajectories with known β; archived in `beta_estimator_calibration.txt`) confirms this reading:
AR(1) returns < 1 even at true β = 2 (0.93–1.00); under engine-like branch saturation the modal
estimator compresses to ≈ 1.05 *regardless* of true β ∈ [1.5, 2] — matching our measured 1.047 —
while a subcritical control (β = 0.8) yields only 29–38% extreme windows and local slope < 1,
versus 83–100% and ≈ 1.9 when supercritical (measured: 69% and 1.6–2.3). The data are thus
consistent with a genuinely supercritical system and inconsistent with a subcritical one.
The conclusion is insensitive to this spread: even the largest
estimate gives g\* = 4(β̂−1) ≈ 5.1, and the deployed g = 22 clears g\* for every β up to 6.5 — the
feedback sits far above the critical gain, which is exactly why closed-loop possession seats firmly
in band (§7).

![Beta ablation](figures/fig_beta_ablation.png)
**Figure 6.** Leading-side share distributions (windows of W = 100 held frames). (a) Open loop
(g = 0, six ablation matches): mass drifts to the extremes — 69% of windows at ρ ≤ 0.35 or ρ ≥ 0.65,
only 5% central — the bimodal signature Theorem 1 predicts for β > 1. (b) Closed loop (g = 22,
the two balanced fixtures): unimodal, 11% extreme / 33% central; the monopoly zone (shaded) stays
empty.

### 6.3 Formations cannot collapse

Off-ball motion blends target and anchor: `xᵢᵗ⁺¹ = (1−η)xᵢᵗ + η[(1−λ)φᵢ + λzᵢᵗ]` (eq. 2).

> **Proposition 2 (non-collapse).** If all agents target a common point (worst case), with idiosyncratic
> heterogeneity of cross-player variance σ_z², the stationary dispersion is
> `S∞² = (1−λ)²S² + λ²σ_z² ≥ (1−λ)²S²`. For any λ < 1 the formation cannot collapse;
> collapse needs λ = 1 and σ_z = 0 (measure zero).

![Dispersion](figures/fig_dispersion.png)
**Figure 7.** Intent anchoring guarantees dispersion ≥ (1−λ)²S² for any λ < 1 (Prop. 2). SFAS's
two-stage anchoring (λ₁ = 0.45, λ₂ = 0.40) keeps ≈11% of formation spread even under universal
ball-chasing.

### 6.4 Calibration as an outer loop

The control laws are tuned so aggregate statistics land in real-football bands (Table 1). Two
couplings: possession share *and* sequence count are both governed by Theorem 2; shot statistics are
meaningful only because Prop. 2 keeps the formation intact.

**Table 1. Realism bands and governing law.**

| Statistic | Band | Governing law |
|---|---|---|
| Possession % | 30–70 | Thm. 2, Cor. |
| Shots | 5–20 | shot gate + dispersion |
| Shots on target | 25–50% | finishing contest |
| Save rate | 60–80% | goalkeeper contest |
| Pass completion | 50–90% | contextual interception (measured per-attempt) |
| Offsides | 0–5 | line geometry |
| Sequences | 60–220 | Thm. 2 (turnover rate) |
| Goals | 0–7 | finishing contest (Skellam diff.) |
| xG | consistency with goals | shot-quality model |
| Recovery (s) | 5–60 | turnover hazard |

## 7. Experiments

**Setup.** Adapters at rank 16 over one shared base, from per-team datasets (≈2400 each) for the 48
World-Cup finalists. Per-fixture possession anchors are compiled from both teams' real style vectors by the
learned calibration policy of §6.2 (CEM-fitted, archived in `calib_policy.json`) and enforced
softly (k_p = 0.4 director, weakened lean, style→behaviour gains). ≈4000 frames/match; one
shared on-ball brain (run as **Nemotron-3 Super** and, on a second machine, **Qwen3.5-35B-A3B**, with
consistent behaviour) + two off-ball **Gemma-4-E2B** daemons; the backbone is interchangeable. Every
match archived (trajectory, 11-stat line, config,
replay); all figures reproduce. The corpus is **75 matches over seven fixtures** spanning the
strength spectrum: *Brazil–Japan*, *Côte d'Ivoire–Norway*, *Mexico–Ecuador*, *Spain–Austria*,
*Belgium–Senegal*, *Argentina–Cape Verde* (ten each) and a fifteen-match *Paraguay–France* series;
new fixtures fold in additively under the same schema.

**Statistical realism (Fig. 8).** Across all 75 matches the aggregate statistics sit in their bands:
pass completion is in-band in **all 150 team-matches** (64.6–87.4%, mean 76.3%); the dominant side's
possession scales with the strength gap (means 46–60% for balanced pairs, up to 68–69% for the heavy
mismatches) and stays inside 30–70% in every match; sequences, offsides and recovery track their
bands; goals and xG are consistent (149 goals vs 207 xG; −0.39 goals−xG per team-match — finishing
is noisy and slightly conservative). Two understood per-match exceptions: (1) shot count exceeds the
conservative 5–20 band in the highest-tempo matches (open games produce 25+ shots, faithful to real
football) and falls below it for parked-bus sides in mismatches; (2) save rate, a ratio over the few
shots on target per match, is high-variance and reaches 100% in low-shot games (a small-denominator
effect, like a real clean sheet). Both are small-count effects, not dynamics; the bands give
*typical* values, which the means respect.

![Stat bands](figures/fig_stat_bands.png)
**Figure 8.** Six representative statistics (of eleven) across the seven-fixture, 75-match corpus,
per team, vs. realism bands. Means lie inside every band; the shot-band excursions are the
highest-tempo matches and the deepest defensive blocks.

**Possession in action (Figs. 9–10).** Within one match (Fig. 9 — the Paraguay–France 1–0 upset),
the dominant side's cumulative possession converges to 69.5% while the EMA fluctuates around it and,
after an early transient, **never re-enters** the >80% monopoly region the open-loop dynamics drifts
to — the empirical face of Theorem 2, in the hardest case (a heavy mismatch). Across all 75 matches
(Fig. 10) the dominant side's seat scales smoothly with the strength gap — 52–60% in balanced
fixtures, 65–70% in the heaviest mismatches — and never becomes a monopoly. Two checks close the
loop: **venue symmetry** (the strong side seats at 68.2% when at home, Argentina, and 68.6% when
away, France — a 0.5-point difference, so the seat is strength, not venue) and **possession ≠
outcome** (France conceded 69.5% possession and lost Fig. 9's match).

![Timeline](figures/fig_timeline.png)
**Figure 9.** The dominant side within one match (Paraguay–France, 1–0 upset). France's cumulative
possession (dark) converges to 69.5%; the EMA (light) fluctuates with the predicted O(√α) amplitude
and, after the opening transient, stays out of the open-loop monopoly attractor (shaded) — and
France still loses.

![Possession per match](figures/fig_possession.png)
**Figure 10.** Per-match possession across all seven fixtures. The dominant side's seat scales with
the strength gap; neither team approaches the monopoly region; every match stays inside the 30–70%
band.

**Competitive balance and finishing (Figs. 11–12).** Records track strength without becoming
scripted (Fig. 11): balanced fixtures split (Brazil–Japan 3-4-3, Mexico–Ecuador 2-6-2) while the
stronger side dominates the mismatches (Spain 7-2-1, Belgium and Argentina 8-1-1, France 8-5-2
*away*) — yet the clearly weaker side still wins 9 of 65 unbalanced matches (14%), the realistic
upset rate the stabilisation law makes possible: dominated sides stay in matches instead of being
run over. Overall 36-22-17 to the home side with 149 goals and no blowout artefacts. Shot volume
tracks territorial control, and realised goals scatter around the xG diagonal (Fig. 12) with a
conversion slightly below one — noisy, mildly conservative finishing. A representative match is in
Table 2.

![Results](figures/fig_results.png)
**Figure 11.** (a) Per-fixture records (home wins / draws / away wins). (b) The Paraguay–France
scorelines (2-5-8, aggregate 5–14): France dominates but Paraguay wins twice and draws five times.

![Shots and xG](figures/fig_shots_xg.png)
**Figure 12.** (a) Shots per match by team across fixtures. (b) Realised goals vs. xG over all 150
team-matches; points scatter around the diagonal with conversion slightly below one.

**Table 2. A representative Paraguay–France match (0–1): a heavy stylistic mismatch, in band.**

| Statistic | Paraguay | France |
|---|---|---|
| Possession | 32.4% | 67.6% |
| Shots | 3 | 13 |
| Shots on target | 33.3% | 38.5% |
| Save rate | 80.0% | 100.0% |
| Pass completion | 68.2% | 87.4% |
| Passes | 192 | 412 |
| Offsides | 1 | 6 |
| Sequences | 141 | 142 |
| Goals | 0 | 1 |
| xG | 0.34 | 1.64 |
| Mean recovery (s) | 25.9 | 12.3 |

**Style identifiability.** Measured over 150 match-identical off-ball probes (the deployed prompt and
schema; K = 20 sampled decisions per probe per team; split-half self-TV noise floor 0.42): all 1,128
pairwise decision TVs exceed the 0.3 gate — minimum 0.45 (Bosnia–South Africa, two low-block styles),
median 0.78 — with 98% of pairs separated from the noise floor by more than 0.10 and a median
per-team nearest-neighbour distance of 0.59 (Fig. 13). By Proposition 3 (§8.1) the teams are
behaviourally distinguishable from a constant number of probes — the adapters encode distinct styles
rather than collapsing onto the shared base (archived in `style_distance_matrix.json`).

![Style heatmap](figures/fig_style_heatmap.png)
**Figure 13.** Measured 48×48 pairwise decision-TV matrix over 150 match-identical off-ball probes
(diagonal = 0). All 1,128 pairs exceed the 0.3 gate; the marked cell is the closest pair
(Bosnia–South Africa, two low-block styles, TV = 0.45 vs noise floor 0.42).

**Backbone robustness.** The design is backbone-agnostic
(§3), so we run two sweeps over the series, holding everything else fixed. *Brain sweep:* the brain
swapped across [Qwen3.5-35B-A3B, Nemotron-3 Super, Llama-4-Maverick, MiniMax-M1] (players fixed).
*Player sweep:* the team adapters re-distilled onto [Gemma-4-E4B, Qwen3.5-9B, GLM-4-9B, InternLM3-8B,
MiMo-7B, Hunyuan-7B] — a 3–10B band from six labs (Google, Alibaba, Zhipu, Shanghai AI Lab, Xiaomi,
Tencent), brain fixed. **(i) Invariance** (Fig. R1): across backbones possession sits at 59.2±2.0%
with no monopoly, all aggregate stats stay in-band, the across-backbone spread of every metric
(0.9 pp for possession) is within the match-to-match SD, per-statistic differences vs the deployed
brain all straddle zero (Fig. R3), and Kruskal–Wallis does not reject equality (p ≥ 0.6 for every
metric). **(ii) Perturbation ordering**
(Fig. R2a): swapping the backbone shifts the decision distribution by only 0.06±0.02 TV — below the
between-team distance 0.34 and the 0.3 gate — so the model perturbs play less than the team identity
(§8.1). **(iii) Cost, not behaviour** (Fig. R2b): the backbone sets schema-validity (96.8–99.8%) and
latency (60–560 ms/decision) but not the emergent match; every backbone cleared the validity needed
for stable play. Model choice is a cost/latency decision, not a correctness one.

![Backbone invariance](figures/fig_backbone_invariance.png)
**Fig. R1.** Emergent stats under four brain backbones (players fixed); red bars = per-arm means,
shading = realism bands. The arms are statistically interchangeable in every panel: possession
means span 58.7–59.6% (spread 0.9 pp vs 1.7 pp match-to-match SD), shots 13.5–15.8 (2.3 vs 7.5),
pass completion 80.7–82.0% (1.4 vs 2.3), total goals 1.7–2.4 (0.7 vs 1.7); no arm leaves its band
and Kruskal–Wallis cannot separate them (p ≥ 0.6) — the invariants are properties of the control
laws, not the model.

![Backbone ordering and cost](figures/fig_backbone_ordering.png)
**Fig. R2.** (a) A backbone swap moves the decision distribution 0.06±0.02 TV (every point below
the 0.3 gate) vs 0.34±0.04 for a team swap — a ~5× separation: model choice perturbs play about
one-fifth as much as team identity. (b) Cost does depend on the backbone: latency spans an order
of magnitude (60–560 ms/decision) and validity 96.8–99.8%; the brain tier trades speed for
reliability (Nemotron-3 560 ms/99.8%; deployed Qwen3.5-35B 180 ms/99.1%), the 3–10B player tier
clusters at 60–105 ms with 96.8–99.2% — every backbone clears the validity needed for stable play.

![Backbone forest](figures/fig_backbone_forest.png)
**Fig. R3.** Per-statistic mean difference of each alternative brain vs the deployed Qwen3.5-35B
(95% CI); every interval straddles zero — no metric distinguishes the backbones.

## 8. Discussion

### 8.1 Style as an identifiable low-rank object

For teams τ, τ′ and probe distribution D, `D(τ,τ′) = E_w TV(π_τ, π_τ′)`, `D_τ = min_{τ′} D(τ,τ′)`.

> **Proposition 3 (identifiability).** A maximum-likelihood decoder on n i.i.d. probe decisions errs
> with probability ≤ (T−1)·exp(−c·n·τ₀²) whenever D_τ > τ₀. The 0.3 deployment gate is exactly this
> hypothesis — it guarantees the adapters are behaviourally separable.

### 8.2 Limitations

(i) **Residual home/away asymmetry** the set-point absorbs but does not fully explain. (ii) **Pass
completion, now contextual (marking, pressure, distance) and measured per-attempt, still uses a
skill-anchored base** — deriving it fully from ball-flight interception geometry is future work.
(iii) **Scalar mean field** — a joint possession–territory
analysis is open. (iv) **Distinct ≠ correct** vs real tactical ground truth. (v) **Corpus breadth** —
75 matches over seven fixtures; more fixtures and tournament-length runs fold in additively.

## 9. Conclusion

SFAS is built on a small idea: 22 isolated agents act on one shared world map, a deterministic operator
resolves them, team identity is a hot-swappable LoRA, tactics are grounded in a knowledge graph. As a
*system*, it shows coordinated, stylistically diverse, statistically realistic football can emerge from
message-free LLM agents with interpretable, parameter-level control. As an *analysis*, it shows the
emergent invariants are governed by identifiable control laws with sharp thresholds — a pitchfork that
makes possession monopoly generic, a critical gain g\* = 4(β−1) that removes it, a convex-anchoring
bound (1−λ)²S² that keeps formations intact — rather than by tuning. The lesson generalises: when many
LLM agents share a world, the object worth designing and analysing is the emergent invariants of the
coupled system, which yield to the dynamical-systems and stochastic-approximation tools applied
probability already provides.

---

## Appendix A. Proofs (sketches)

**Mean-field reduction.** Eq. (1) is Robbins–Monro with constant gain α; by the ODE method (Benaïm
1999) the interpolation tracks `ρ̇ = m(ρ)`, converges to stable fixed points of m, avoids unstable
ones a.s.; stationary fluctuation variance `α q(ρ*)(1−q(ρ*))/(2|m′(ρ*)|) + O(α²)` → O(√α). **Thm. 1.**
`q′(½) = β`, `m′(½) = β−1`; normal form `m = (β−1)ε − ((4β)³/48)ε³` — supercritical pitchfork. **Thm.
2.** `ℓ′(½⁺) = −4β + g` ⇒ `m̃′(½) = β−1−g/4 < 0 ⟺ g > 4(β−1)`. **Cor.** asymmetry shifts q̃(½) by
¼(η_H+η_A); closed-loop slope −(g−g\*)/4 ⇒ ρ†. **Prop. 2.** affine contraction toward
(1−λ)φᵢ + λzᵢ; common-point targets have cross-player variance (1−λ)²S². **Prop. 3.** LLR error ≤
Chernoff ≤ const·D²; union bound over T−1 rivals.

## Appendix B. Implementation constants
g = 22; α = 0.02 (window ≈50); ρ* = 0.55; λ₁ = 0.45, λ₂ = 0.40; LoRA rank 16; SFT ≈2400/team;
≈4000 frames/match; T = 48 teams; distinctness gate 0.3.

## References

Baker et al. (2020) ICLR · Benaïm (1999) Sém. Prob. XXXIII · Berner et al. (2019) arXiv:1912.06680 ·
Bernstein et al. (2002) Math. OR 27(4) · Dettmers et al. (2023) NeurIPS · Dixon & Coles (1997) JRSS-C
46(2) · Edge et al. (2024) arXiv:2404.16130 · Hu et al. (2021) arXiv:2106.09685 · Hunter (2007) CiSE
9(3) · Kitano et al. (1997) Autonomous Agents · Kurach et al. (2020) AAAI · Kushner & Yin (2003)
Springer · Lasry & Lions (2007) Jpn. J. Math. 2(1) · Lewis et al. (2020) NeurIPS · Li et al. (2023)
NeurIPS · Maher (1982) Stat. Neerl. 36(3) · Park et al. (2023) UIST · Pemantle (2007) Probab. Surveys
4 · Skellam (1946) JRSS 109(3) · Strogatz (1994) Addison-Wesley · Theraulaz & Bonabeau (1999) Artif.
Life 5(2) · Vinyals et al. (2019) Nature 575 · Wang et al. (2023) arXiv:2308.11432 · Wang et al. (2023)
arXiv:2305.16291 · Yang et al. (2018) ICML · Yao et al. (2023) ICLR
