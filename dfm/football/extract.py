"""
football/extract.py — turn microfootball sim records into the DFM training corpus.

Reads the (read-only) synced sim records and produces, per sim:
  * a segment-level stat tensor  X[seg, side, channel]   (the diffusion object)
  * a match-level condition vector c (tactics + rating gap; covariate anchor)
  * an event table (goals w/ scorer+minute, corners, freekicks, offsides,
    penalties, injuries, cards-if-ever-present) for the Poisson event layer

Side attribution: events are name-based, no roster file exists — so we attribute
via (a) explicit team names in the event string (goals/corners/freekicks/penalties)
and (b) the ball-possession side of the surrounding frames for everything else.
Extraction is cross-checked against each sim's own stats.json (possession, shots,
passes, offsides, goals) and the mismatch report is part of the output.

Writes ONLY inside dfm/football/data/.

Usage:  python extract.py [--sims-root <dir>] [--segments 9]
"""
import argparse
import glob
import json
import os
import re

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.normpath(os.path.join(
    HERE, '..', '..', 'someo-park-investment-management', 'server', 'sim_assets', 'microfootball'))
OUT_DIR = os.path.join(HERE, 'data')

# channel names, per side per segment (order matters — the model consumes this)
CHANNELS = [
    'poss_share',      # share of held-ball ticks
    'passes',          # pass attempts
    'pass_comp_rate',  # completed / attempted
    'shots',
    'shots_on_target',
    'goals',
    'corners',
    'freekicks',
    'offsides',
    'turnovers_won',   # possession recoveries
    'tackles',
    'field_tilt',      # mean |ball y - pitch center| (normalized 0-1) while holding.
                       # Ends swap at halftime (shot y is bimodal for BOTH sides), so raw
                       # mean-y is direction-ambiguous; center-distance = how deep play is,
                       # label-neutral and safe under home/away flip augmentation.
    'red_cards',       # red card freezes the player's pos to ['NP','NP'] — the first NP
                       # frame IS the sending-off. NEW-format sims also emit an explicit
                       # 'Red card: <player>' event (parsed too; deduped by NP frame).
    'yellow_cards',    # NEW-format sims log 'Yellow card: <player>' in the event stream
                       # (the box-B card-visibility fix). Old sims emitted no visible
                       # yellows → this channel is 0 there (backward-compatible).
    'xg',              # stats.json match xG allocated across segments by shot share
                       # (plan §2 option 2 — shot positions would need a shot-quality model)
    'sprints',         # sprint-action player-frames per side (attributed via the player's
                       # own team, not possession)
    'sequences',       # possession spells STARTED by this side in the segment (holder-side
                       # changes) — completes the engine's 11-stat line natively
    'save_pct',        # goalkeeper save rate (fraction 0-1). A MATCH-LEVEL aggregate
                       # (per-segment save% is undefined when the keeper faced 0 shots on
                       # target), so replicated across segments; diffused as a rate (logit)
                       # so it joins the factor structure (save% ↔ goals-conceded coupling).
    'recovery_sec',    # avg ball-recovery time (seconds, ~5-60). MATCH-LEVEL; replicated
                       # across segments; log-transformed level (not a count-sum). A
                       # pressing proxy — low recovery ↔ high press ↔ more turnovers/corners.
]
# match-level (replicated across segments) — do NOT sum over segments to reconstruct
LEVEL_CHANNELS = ('save_pct', 'recovery_sec')
# discrete event types kept for the Poisson layer (cards auto-activate if the
# engine ever starts emitting them — the regexes are already in place)
EVENT_TYPES = ['goal', 'corner', 'freekick', 'offside', 'penalty', 'injury', 'card']

# Static strength covariate (z-score-ish, FIFA-ranking flavored). The sims' own
# rating text is free-form LLM prose and unparseable; a constant table is stable,
# leak-free, and — crucially — defined for pairings that have never been simmed.
# Extend when new teams appear (extract warns on misses).
TEAM_STRENGTH = {
    'Argentina': 1.6, 'France': 1.5, 'Spain': 1.5, 'England': 1.3, 'Brazil': 1.2,
    'Belgium': 1.0, 'Mexico': 0.5, 'Japan': 0.5, 'United States': 0.4,
    'Senegal': 0.4, 'Norway': 0.4, 'Austria': 0.3, 'Ecuador': 0.3,
    'Paraguay': 0.0, "Cote d'Ivoire": 0.0, 'Cape Verde': -0.8,
    'Morocco': 0.6,
    # 2026-08-12 (Germany_vs_Paraguay / South Africa_vs_Canada series): the first
    # onlining ran these at the 0.0 fallback — Germany a top side, South Africa
    # well below mid — and the double-miss case only warned for the HOME side, so
    # Canada was silently 0.0 too. Same FIFA-flavored scale as the rows above.
    # Measured impact (correcting commit 7d38d8d's wording): the generator DOES
    # condition on ratings (2 of 13 cond dims + gap; simmed pairings draw conds
    # from their own config pool, TEAM_STRENGTH backs never-simmed pairs via the
    # ridge strength->tactics path), but re-running with real values moved no
    # anchored W/D/L above 2dp — 10/305 corpus rows changed and the learned
    # rating sensitivity is small (model.py:56: the brain compiles tactics from
    # its OWN internal ratings, which disagree with any external table anyway).
    'Germany': 1.2, 'Canada': 0.3, 'South Africa': -0.3,
    # 2026-09-01 (the four new matchups): SEVEN sides ran the first onlining at the
    # 0.0 fallback — Portugal/Netherlands top-tier, Bosnia well below mid — the exact
    # repeat of the 08-12 Germany/Canada episode. Same FIFA-flavored scale; per the
    # 08-12 measured-impact note, expect anchored W/D/L to move <2dp, and this row
    # exists for hygiene + the never-simmed ridge path, not because numbers will jump.
    'Portugal': 1.3, 'Netherlands': 1.2, 'Croatia': 1.0,
    'Sweden': 0.3, 'Australia': 0.2, 'Egypt': 0.2,
    'Bosnia and Herzegovina': -0.2,
}

RE_GOAL = re.compile(r'^Goal Scored by - (.+) - \((.+)\)$')
RE_CORNER = re.compile(r'^Corner to - (.+)$')
RE_FREEKICK = re.compile(r'^freekick to: (.+?) \[')
RE_PENALTY = re.compile(r'^penalty to: (.+)$')
RE_OFFSIDE = re.compile(r'^(.+) is offside$')
RE_INJURY = re.compile(r'^Player Injured - (.+)$')
RE_CARD = re.compile(r'(yellow card|red card|booking|sent off|card shown)', re.I)
# NEW-format explicit card events: "Yellow card: A. El Kaabi" / "Red card: X"
RE_YELLOW = re.compile(r'^Yellow card:\s*(.+)$', re.I)
RE_RED = re.compile(r'^Red card:\s*(.+)$', re.I)
RE_SHOT = re.compile(r'^Shot Made by: (.+)$')
RE_SHOT_ON = re.compile(r'^Shot On Target by (.+)$')
RE_PASS = re.compile(r'^(ball passed by|ball crossed by|through ball attempted by): (.+)$')
RE_PASS_DONE = re.compile(r'^pass completed to (.+)$')
RE_POSS_WON = re.compile(r'^Possession won by (.+)$')
RE_TACKLE = re.compile(r'^(Tackle attempted by|Slide tackle attempted by): (.+)$')


def parse_sim(sim_dir: str, n_seg: int):
    """One sim -> (tensor (n_seg, 2, n_channels), condition dict, event list, check dict)."""
    cfg = json.load(open(os.path.join(sim_dir, 'config.json')))
    mcfg = json.load(open(os.path.join(sim_dir, 'match_config.json')))
    stats = json.load(open(os.path.join(sim_dir, 'stats.json')))
    home_name, away_name = cfg['home'], cfg['away']
    side_of_team = {home_name: 0, away_name: 1}

    C = len(CHANNELS)
    X = np.zeros((n_seg, 2, C))
    poss_ticks = np.zeros((n_seg, 2))
    tilt_sum = np.zeros((n_seg, 2))
    events = []
    y_span = [np.inf, -np.inf]

    # ball.team is never populated in the trajectories — possession is ball.holder
    # (a player id) resolved through the per-frame players[].team. state.json's
    # poss_*_ticks equals exactly the count of holder-set frames, confirming this.
    id_side = {}                # player id -> 0/1, filled lazily from frames
    ball_side = None            # 0/1/None — sticky possession side (last holder's side)
    sent_off = set()            # player ids already counted as red-carded
    yellow_minutes = []         # minutes of 'Yellow card:' events (new-format sims)
    offside_minutes = []        # minutes of 'X is offside' events (side comes from stats.json)
    with open(os.path.join(sim_dir, 'trajectory.jsonl')) as f:
        for line in f:
            fr = json.loads(line)
            minute = fr.get('min', 0.0)
            seg = min(int(minute // (90.0 / n_seg)), n_seg - 1)
            if len(id_side) < 22:
                for p in fr.get('players', []):
                    id_side[p['id']] = 0 if p['team'] == 'home' else 1
            # red card = pos frozen to ['NP','NP'] (the engine's send-off marker)
            for p in fr.get('players', []):
                if p['id'] not in sent_off and isinstance(p['pos'][0], str):
                    sent_off.add(p['id'])
                    side = id_side.get(p['id'])
                    if side is not None:
                        X[seg, side, CHANNELS.index('red_cards')] += 1
                        events.append({'type': 'card', 'card': 'red',
                                       'min': round(minute, 1), 'side': side})
            for p in fr.get('players', []):
                if p.get('action') == 'sprint' and p['id'] in id_side:
                    X[seg, id_side[p['id']], CHANNELS.index('sprints')] += 1
            holder = fr['ball'].get('holder')
            if holder is not None and holder in id_side:
                new_side = id_side[holder]
                if new_side != ball_side:
                    X[seg, new_side, CHANNELS.index('sequences')] += 1
                ball_side = new_side
                poss_ticks[seg, ball_side] += 1
                try:
                    y = float(fr['ball']['pos'][1])
                except (TypeError, ValueError, IndexError):
                    y = None
                if y is not None:
                    tilt_sum[seg, ball_side] += y
                    y_span[0], y_span[1] = min(y_span[0], y), max(y_span[1], y)

            for e in (fr.get('events') or []):
                s = e if isinstance(e, str) else str(e)
                m = RE_GOAL.match(s)
                if m:
                    side = side_of_team.get(m.group(2))
                    if side is not None:
                        X[seg, side, CHANNELS.index('goals')] += 1
                        events.append({'type': 'goal', 'min': round(minute, 1), 'side': side,
                                       'player': m.group(1)})
                    continue
                m = RE_CORNER.match(s)
                if m:
                    side = side_of_team.get(m.group(1))
                    if side is not None:
                        X[seg, side, CHANNELS.index('corners')] += 1
                        events.append({'type': 'corner', 'min': round(minute, 1), 'side': side})
                    continue
                m = RE_FREEKICK.match(s)
                if m:
                    side = side_of_team.get(m.group(1))
                    if side is not None:
                        X[seg, side, CHANNELS.index('freekicks')] += 1
                        events.append({'type': 'freekick', 'min': round(minute, 1), 'side': side})
                    continue
                m = RE_PENALTY.match(s)
                if m:
                    side = side_of_team.get(m.group(1))
                    if side is not None:
                        events.append({'type': 'penalty', 'min': round(minute, 1), 'side': side})
                    continue
                m = RE_YELLOW.match(s)
                if m:
                    # count + minute kept; per-side attribution comes from stats.json below
                    # (frames carry no player name → we can't map the name to a side here)
                    yellow_minutes.append(round(minute, 1))
                    events.append({'type': 'card', 'card': 'yellow', 'min': round(minute, 1),
                                   'side': ball_side, 'player': m.group(1)})
                    continue
                m = RE_RED.match(s)
                if m:
                    # explicit red event (new format); the NP-freeze path already counted
                    # it into the channel, so only record the event if not yet seen
                    events.append({'type': 'card', 'card': 'red', 'min': round(minute, 1),
                                   'side': ball_side, 'player': m.group(1), 'explicit': True})
                    continue
                if RE_CARD.search(s):
                    events.append({'type': 'card', 'min': round(minute, 1), 'side': ball_side,
                                   'raw': s[:80]})
                    continue
                if RE_INJURY.match(s):
                    events.append({'type': 'injury', 'min': round(minute, 1), 'side': ball_side})
                    continue
                # possession-side attributed events (actor holds / is closest to the ball)
                if ball_side is None:
                    continue
                if RE_SHOT.match(s):
                    X[seg, ball_side, CHANNELS.index('shots')] += 1
                elif RE_SHOT_ON.match(s):
                    X[seg, ball_side, CHANNELS.index('shots_on_target')] += 1
                elif RE_PASS.match(s):
                    X[seg, ball_side, CHANNELS.index('passes')] += 1
                elif RE_PASS_DONE.match(s):
                    X[seg, ball_side, CHANNELS.index('pass_comp_rate')] += 1   # numerator, ratio below
                elif RE_OFFSIDE.match(s):
                    # count + minute only: the penalised side is NOT recoverable from the
                    # trajectory (frames carry player ids but no names, and no roster file
                    # exists, so 'X is offside' can't be mapped to a team). Possession at
                    # the whistle agrees with stats.json only ~85% of the time, and no
                    # frame-timing variant does better — measured over all 275 sims,
                    # total |per-side error|: holder-at-event 604, holder-1 1454,
                    # holder-2 604, inverted 1470 (event COUNT is exact: 1973 = 1973).
                    # stats.json carries the engine's authoritative per-side count →
                    # allocate below, same pattern as yellow_cards.
                    offside_minutes.append(round(minute, 1))
                    events.append({'type': 'offside', 'min': round(minute, 1), 'side': ball_side})
                elif RE_POSS_WON.match(s):
                    # the winner is the *new* holder — ball.team may lag one frame, so
                    # attribute to the opposite of the pre-event holder
                    X[seg, 1 - ball_side, CHANNELS.index('turnovers_won')] += 1
                elif RE_TACKLE.match(s):
                    X[seg, 1 - ball_side, CHANNELS.index('tackles')] += 1

    # ratios & shares from the raw tallies
    ip, is_ = CHANNELS.index('poss_share'), CHANNELS.index('pass_comp_rate')
    ipass = CHANNELS.index('passes')
    itilt = CHANNELS.index('field_tilt')
    tot = poss_ticks.sum(1, keepdims=True) + 1e-9
    X[:, :, ip] = poss_ticks / tot
    comp = X[:, :, is_].copy()
    X[:, :, is_] = comp / np.maximum(X[:, :, ipass], 1)
    y_lo, y_hi = (y_span if y_span[0] < y_span[1] else (0.0, 1.0))
    y_mid, y_half = (y_lo + y_hi) / 2.0, max((y_hi - y_lo) / 2.0, 1e-9)
    with np.errstate(invalid='ignore'):
        mean_y = tilt_sum / np.maximum(poss_ticks, 1)
        X[:, :, itilt] = np.where(poss_ticks > 0,
                                  np.abs(mean_y - y_mid) / y_half, 0.5)

    # xG: allocate the match total across segments by shot share (uniform if shotless)
    ixg, ish = CHANNELS.index('xg'), CHANNELS.index('shots')
    for si, side in enumerate(('home', 'away')):
        tot_sh = X[:, si, ish].sum()
        share = X[:, si, ish] / tot_sh if tot_sh > 0 else np.full(n_seg, 1.0 / n_seg)
        X[:, si, ixg] = float(stats[side].get('xg') or 0.0) * share

    # yellow cards (NEW format): stats.json carries the authoritative per-side count;
    # distribute each side's total across segments by where the trajectory's
    # 'Yellow card:' events landed (uniform over played segments if none observed).
    iyc = CHANNELS.index('yellow_cards')
    if yellow_minutes:
        yc_seg = np.zeros(n_seg)
        for mnt in yellow_minutes:
            yc_seg[min(int(mnt // (90.0 / n_seg)), n_seg - 1)] += 1
        yc_share = yc_seg / yc_seg.sum()
    else:
        yc_share = np.full(n_seg, 1.0 / n_seg)
    for si, side in enumerate(('home', 'away')):
        n_yellow = float(stats[side].get('yellow_cards') or 0.0)
        X[:, si, iyc] = n_yellow * yc_share

    # offsides: identical treatment — the trajectory times the whistles correctly but
    # cannot attribute them to a side (see the RE_OFFSIDE branch), so take each side's
    # total from stats.json and spread it over the segments where offsides were observed.
    iof = CHANNELS.index('offsides')
    if offside_minutes:
        of_seg = np.zeros(n_seg)
        for mnt in offside_minutes:
            of_seg[min(int(mnt // (90.0 / n_seg)), n_seg - 1)] += 1
        of_share = of_seg / of_seg.sum()
    else:
        of_share = np.full(n_seg, 1.0 / n_seg)
    for si, side in enumerate(('home', 'away')):
        n_off = float(stats[side].get('offsides') or 0.0)
        X[:, si, iof] = n_off * of_share

    # match-level rate/aggregate channels: replicate the stats.json value across segments.
    # save_pct null = keeper faced 0 shots on target (0/0) → corpus-typical 0.70.
    isv, irec = CHANNELS.index('save_pct'), CHANNELS.index('recovery_sec')
    for si, side in enumerate(('home', 'away')):
        sv = stats[side].get('save_pct')
        X[:, si, isv] = (float(sv) / 100.0) if sv is not None else 0.70
        rec = stats[side].get('recovery_sec')
        X[:, si, irec] = float(rec) if rec is not None else 20.0

    # ---- cross-check against the sim's own stats.json ----
    check = {}
    for si, side in enumerate(('home', 'away')):
        st = stats[side]
        seg_poss = float((poss_ticks[:, si].sum() / poss_ticks.sum()) * 100)
        check[f'{side}_poss'] = (round(seg_poss, 1), st['possession_pct'])
        check[f'{side}_shots'] = (int(X[:, si, CHANNELS.index('shots')].sum()), st['shots'])
        check[f'{side}_passes'] = (int(X[:, si, ipass].sum()), st['passes'])
        # allocated (not tallied) → round, else float residue truncates 6.0 to 5
        check[f'{side}_offsides'] = (round(X[:, si, CHANNELS.index('offsides')].sum()),
                                     st['offsides'])
        check[f'{side}_goals'] = (int(X[:, si, CHANNELS.index('goals')].sum()), st['goals'])
        check[f'{side}_sequences'] = (int(X[:, si, CHANNELS.index('sequences')].sum()),
                                      st.get('sequences'))
        # corners: NEW-format stats.json has the ground-truth count — cross-check the
        # trajectory-extracted total against it (old format lacks the field → skip)
        if 'corners' in st:
            check[f'{side}_corners'] = (int(X[:, si, CHANNELS.index('corners')].sum()),
                                        st['corners'])
        # match-level channels: reconstruct as the segment MEAN (they were replicated)
        sv = st.get('save_pct')
        if sv is not None:
            check[f'{side}_save'] = (round(X[:, si, CHANNELS.index('save_pct')].mean() * 100, 1), sv)
        rec = st.get('recovery_sec')
        if rec is not None:
            check[f'{side}_recovery'] = (round(X[:, si, CHANNELS.index('recovery_sec')].mean(), 1), rec)

    # ---- condition vector: tactics (5 per side) + rating gap from narrative ----
    def tac(side_key):
        t = mcfg[side_key]
        # older configs lack shots_target — corpus median (12) as neutral default
        return [t['directness'], t['possession_target'], t['press_intensity'],
                t['tempo'], t.get('shots_target', 12) / 20.0]
    r_home = TEAM_STRENGTH.get(home_name)
    r_away = TEAM_STRENGTH.get(away_name)
    if r_home is None or r_away is None:
        missing = [n for n, r in ((home_name, r_home), (away_name, r_away))
                   if r is None]                    # BOTH sides, not home-shadows-away
        print(f'  WARNING: team missing from TEAM_STRENGTH: '
              f'{", ".join(missing)} — using 0.0')
        r_home = 0.0 if r_home is None else r_home
        r_away = 0.0 if r_away is None else r_away
    cond = {
        'home_team': home_name, 'away_team': away_name,
        'tactics_home': tac('home'), 'tactics_away': tac('away'),
        'rating_home': r_home, 'rating_away': r_away,
        'score': stats['score'],
    }
    return X, cond, events, check


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sims-root', default=DEFAULT_ROOT)
    ap.add_argument('--segments', type=int, default=9)
    args = ap.parse_args()

    sim_dirs = sorted(glob.glob(os.path.join(args.sims_root, '*', '2*')))
    print(f'[extract] {len(sim_dirs)} sims under {args.sims_root}')
    tensors, conds, all_events, checks = [], [], [], []
    bad = 0
    for sd in sim_dirs:
        try:
            X, cond, events, check = parse_sim(sd, args.segments)
        except Exception as e:
            print(f'  SKIP {os.path.basename(sd)}: {e}')
            bad += 1
            continue
        cond['sim_dir'] = os.path.relpath(sd, args.sims_root)
        tensors.append(X)
        conds.append(cond)
        all_events.append(events)
        checks.append(check)

    X = np.stack(tensors)          # (n_sims, n_seg, 2, n_channels)
    os.makedirs(OUT_DIR, exist_ok=True)
    np.save(os.path.join(OUT_DIR, 'segment_tensor.npy'), X)
    json.dump({'channels': CHANNELS, 'event_types': EVENT_TYPES,
               'segments': args.segments, 'conds': conds},
              open(os.path.join(OUT_DIR, 'conditions.json'), 'w'), indent=1)
    json.dump(all_events, open(os.path.join(OUT_DIR, 'events.json'), 'w'))

    # ---- verification report ----
    print(f'[extract] tensor {X.shape}; {bad} skipped')
    n_cards = sum(1 for evs in all_events for e in evs if e['type'] == 'card')
    print(f'[extract] card events found: {n_cards} '
          f'({"channel active" if n_cards else "engine emits no cards yet — channel dormant"})')
    tol = {'poss': 3.0, 'shots': 2, 'passes': 20, 'offsides': 2, 'goals': 0, 'sequences': 25,
           'corners': 2, 'save': 0.5, 'recovery': 0.5}
    mismatch = 0
    for cond, check in zip(conds, checks):
        for key, (mine, ref) in check.items():
            kind = key.split('_', 1)[1]
            if ref is None:
                continue
            if abs(mine - ref) > tol[kind]:
                mismatch += 1
                print(f'  MISMATCH {cond["sim_dir"]} {key}: extracted {mine} vs stats.json {ref}')
    total_checks = sum(len(c) for c in checks)
    print(f'[extract] stats.json cross-check: {total_checks - mismatch}/{total_checks} within tolerance')


if __name__ == '__main__':
    main()
