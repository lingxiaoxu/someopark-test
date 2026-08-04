"""factor_analysis.py — interrogate the FACTOR structure of the football DFM.

The 'F' in DFM is Factor. In the paper (arXiv 2504.06566) the d-dim vector is R = βF + ε:
a d×k loading β, k latent factors F, idiosyncratic ε. The score decomposes (Lemma 1) into
a k-dim nonlinear part on the factor subspace + an analytic linear complement, so the
network only learns k dims and the sample complexity depends on k not d. In
CondFactorScoreNet, β is `self.V` (d×k, d = n_seg·2·n_channels), z = Vᵀx are the factor
scores, and the complement is −D_t x.

This answers, for the FOOTBALL data, the questions that make the factor structure real
rather than decorative:

  1. HOW MANY factors does the match-stat covariance actually have? (eigenvalue spectrum;
     is the chosen k right, or are we fitting noise / under-fitting?)
  2. WHAT is each factor? (project loading columns back to (segment, side, channel) and
     read which stats co-move — tempo? territorial dominance? physicality?)
  3. HOW MUCH covariance the k-dim subspace captures, and whether the idiosyncratic
     complement (Gaussian σ²) is an honest model of the leftover — the count channels are
     Poisson-like (mean≈var), which a Gaussian ε cannot represent.
  4. WHY it powers cross-fixture transfer: the subspace is SHARED across all matchups;
     only factor scores move with the condition.

Writes football/data/factor_report.{json,txt}.
"""
from __future__ import annotations

import json
import os

import numpy as np
import torch

from model import (build_cfa_beta, build_training_arrays, load_corpus, train_conditional,
                   CFA_NAMES)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')


def eigen_spectrum(Z: np.ndarray) -> dict:
    """Eigenvalue spectrum of the standardized match-stat covariance → how many factors."""
    C = np.corrcoef(Z, rowvar=False)
    C = np.nan_to_num(C, nan=0.0)
    ev = np.sort(np.linalg.eigvalsh(C))[::-1]
    ev = np.clip(ev, 0, None)
    cum = np.cumsum(ev) / ev.sum()
    # Kaiser (eigenvalue>1) and 90%-variance rules bracket the honest factor count
    kaiser = int((ev > 1.0).sum())
    k90 = int(np.searchsorted(cum, 0.90) + 1)
    k80 = int(np.searchsorted(cum, 0.80) + 1)
    return {'eigenvalues': [round(float(e), 3) for e in ev[:15]],
            'cum_var_top15': [round(float(c), 3) for c in cum[:15]],
            'kaiser_k': kaiser, 'k_for_80pct': k80, 'k_for_90pct': k90, 'd': int(Z.shape[1])}


def interpret_factors(V: np.ndarray, channels, n_seg: int, top_n: int = 6) -> list:
    """Each loading column → aggregate |loading| over segments per (side, channel) and
    name the factor by its dominant co-moving stats. V is (d, k), d = n_seg·2·C."""
    C = len(channels)
    k = V.shape[1]
    out = []
    for f in range(k):
        col = V[:, f].reshape(n_seg, 2, C)
        # per-(side,channel) energy across segments; sign = net direction
        energy = np.sqrt((col ** 2).mean(0))           # (2, C)
        signed = col.mean(0)                            # (2, C) mean loading (direction)
        loads = []
        for si, side in enumerate(('home', 'away')):
            for ci, ch in enumerate(channels):
                loads.append((energy[si, ci], np.sign(signed[si, ci]), side, ch))
        loads.sort(reverse=True)
        top = [{'stat': f'{s}:{c}', 'load': round(float(sgn * e), 3)}
               for e, sgn, s, c in loads[:top_n]]
        # a factor is "asymmetric/dominance" if its top loads are one-sided; "match-level"
        # if home & away co-load with the SAME sign (tempo shared by both)
        home_e = sum(e for e, _, s, _ in loads[:top_n] if s == 'home')
        away_e = sum(e for e, _, s, _ in loads[:top_n] if s == 'away')
        asym = abs(home_e - away_e) / (home_e + away_e + 1e-9)
        out.append({'factor': f, 'top_stats': top,
                    'asymmetry': round(float(asym), 2),
                    'kind': 'dominance (one-sided)' if asym > 0.5 else 'match-level (both sides)'})
    return out


def procrustes_interpret(V: np.ndarray, channels, n_seg: int) -> dict:
    """Interpretability at ZERO fidelity cost: rotate the EXPLORATORY factors (best
    transfer) toward the football-semantic targets without changing their span, so
    generation is unaffected — a rotation is a change of basis within span(V). For each
    semantic block target t_s, find the unit vector in span(V) closest to it (cos sim),
    reporting how well the data's own subspace already contains each football concept.
    This is the elegant alternative to arch='cfa': keep the exploratory model, name its
    subspace post-hoc."""
    T, _ = build_cfa_beta(channels, n_seg)               # (d, 6) semantic targets
    # project each semantic target onto span(V); cos-sim of target vs its projection
    VVt = V @ V.T
    out = []
    for s, name in enumerate(CFA_NAMES):
        t = T[:, s]
        proj = VVt @ t
        cos = float(proj @ t / (np.linalg.norm(proj) * np.linalg.norm(t) + 1e-12))
        out.append({'concept': name, 'contained_in_subspace': round(cos, 3)})
    return {'semantic_alignment': out,
            'mean_containment': round(float(np.mean([o['contained_in_subspace'] for o in out])), 3)}


def subspace_capture(Z: np.ndarray, V: np.ndarray) -> dict:
    """Fraction of total match-stat variance living in the k-dim subspace span(V), and the
    idiosyncratic leftover — plus the Poisson-honesty check on count channels."""
    # variance explained by projecting onto V (orthonormal)
    P = Z @ V                                            # (n, k) factor scores
    var_sub = P.var(0).sum()
    var_tot = Z.var(0).sum()
    resid = Z - P @ V.T
    return {'subspace_var_frac': round(float(var_sub / var_tot), 3),
            'idiosyncratic_var_frac': round(float(resid.var(0).sum() / var_tot), 3)}


def poisson_honesty(X: np.ndarray, channels) -> dict:
    """For each COUNT channel, the match-total mean vs variance. Gaussian idiosyncratic
    ε assumes var independent of mean; Poisson counts have var≈mean (Fano≈1), and
    overdispersed ones Fano>1 — quantifies where the Gaussian complement is a stretch."""
    from model import COUNT_CHANNELS
    tot = X.sum(1)                                       # (n, 2, C) match totals
    out = {}
    for ci, ch in enumerate(channels):
        if ch not in COUNT_CHANNELS:
            continue
        v = tot[:, :, ci].ravel()
        m = v.mean()
        if m < 1e-6:
            continue
        out[ch] = {'mean': round(float(m), 2), 'var': round(float(v.var()), 2),
                   'fano': round(float(v.var() / m), 2)}
    return out


def main(factor_dim: int = 8, epochs: int = 3000):
    X, meta = load_corpus()
    channels, n_seg = meta['channels'], meta['segments']
    Z, Cc, scaler, keep = build_training_arrays(X, meta)     # standardized flat matrix
    print(f'[factor] corpus {X.shape} → flat {Z.shape} (d={Z.shape[1]}), k={factor_dim}')

    spec = eigen_spectrum(Z)
    print(f'[factor] spectrum: Kaiser k={spec["kaiser_k"]}, k@80%={spec["k_for_80pct"]}, '
          f'k@90%={spec["k_for_90pct"]} (d={spec["d"]})')

    # train the conditional factor model, extract the LEARNED loading V (d×k)
    model = train_conditional(Z, Cc, factor_dim=factor_dim, epochs=epochs, verbose=False)
    with torch.no_grad():
        V = model.V.detach().cpu().numpy()
        V, _ = np.linalg.qr(V)                           # orthonormalize for interpretation
        V = V[:, :factor_dim]
        sigma2 = np.exp(np.clip(model.log_c.detach().cpu().numpy(), None, 4.0))

    factors = interpret_factors(V, channels, n_seg)
    cap = subspace_capture(Z, V)
    fano = poisson_honesty(X, channels)
    proc = procrustes_interpret(V, channels, n_seg)

    report = {'spectrum': spec, 'subspace_capture': cap, 'factor_dim': factor_dim,
              'factors': factors, 'count_fano': fano, 'procrustes': proc,
              'idiosyncratic_sigma2_range': [round(float(sigma2.min()), 3),
                                             round(float(sigma2.max()), 3)]}
    json.dump(report, open(os.path.join(DATA, 'factor_report.json'), 'w'), indent=1)

    lines = []
    lines.append('==================== DFM FACTOR STRUCTURE (football) ====================')
    lines.append(f'flattened match vector d = {spec["d"]} (= {n_seg} seg × 2 sides × '
                 f'{len(channels)} channels); chosen k = {factor_dim}')
    lines.append(f'spectrum: Kaiser(eig>1) k={spec["kaiser_k"]}  '
                 f'k@80% var={spec["k_for_80pct"]}  k@90% var={spec["k_for_90pct"]}')
    lines.append(f'top eigenvalues: {spec["eigenvalues"][:8]}')
    lines.append(f'k-dim subspace captures {cap["subspace_var_frac"]:.0%} of match-stat '
                 f'variance; idiosyncratic leftover {cap["idiosyncratic_var_frac"]:.0%}')
    lines.append('')
    lines.append('FACTORS (learned V, top co-moving stats — the latent match drivers):')
    for fr in factors:
        tops = ', '.join(f'{s["stat"]}({s["load"]:+.2f})' for s in fr['top_stats'][:5])
        lines.append(f'  F{fr["factor"]} [{fr["kind"]}, asym {fr["asymmetry"]}]: {tops}')
    lines.append('')
    lines.append('SEMANTIC CONTAINMENT (how much of each football concept the EXPLORATORY '
                 'subspace already holds — cos-sim of the semantic target with its projection '
                 f'onto span(V); interpretability at zero fidelity cost). mean {proc["mean_containment"]}:')
    for o in proc['semantic_alignment']:
        lines.append(f'  {o["concept"]:12} {o["contained_in_subspace"]:.2f}')
    lines.append('')
    lines.append('POISSON-HONESTY of the Gaussian complement (count channels; Fano=var/mean,'
                 ' 1=Poisson, >1 overdispersed — a Gaussian ε cannot encode this):')
    for ch, s in sorted(fano.items(), key=lambda kv: -kv[1]['fano']):
        lines.append(f'  {ch:16} mean {s["mean"]:6.2f}  var {s["var"]:7.2f}  Fano {s["fano"]}')
    txt = '\n'.join(lines)
    open(os.path.join(DATA, 'factor_report.txt'), 'w').write(txt)
    print('\n' + txt)
    print(f'\nsaved → {DATA}/factor_report.{{json,txt}}')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--k', type=int, default=8)
    ap.add_argument('--epochs', type=int, default=3000)
    a = ap.parse_args()
    main(factor_dim=a.k, epochs=a.epochs)
