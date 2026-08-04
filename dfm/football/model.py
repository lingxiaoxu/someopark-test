"""
football/model.py — conditional diffusion factor model over segment-level match tensors.

The diffusion object is the flattened, variance-stabilized segment tensor
x ∈ R^(n_seg*2*n_channels) from extract.py. Conditioning c is the covariate anchor
(tactics 5+5, ratings 2, rating gap 1 = 13 dims) — team identity enters ONLY through
covariates, never a free embedding: with 16 teams / 9 fixtures the bipartite graph is
too sparse for identifiable embeddings, and covariates are what transfers to unseen
pairings.

Architecture mirrors the paper's Lemma-1 score (FactorScoreNetwork in ../score_network.py):
score(x,t,c) = α_t D_t V g_ζ(Vᵀ D_t x, t, c) − D_t x, with V warm-started from PCA.
The only change vs the parent repo is that c is concatenated into g_ζ's input.

Home/away flip augmentation doubles the corpus: the engine has no home advantage, so
swapping side labels (and mirroring field tilt to keep "toward opponent goal" semantics)
is an exact symmetry.
"""
import json
import os
import sys

import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(HERE))             # parent dfm/ modules (append —
                                                    # never shadow football/model.py)
from score_network import TimeEmbedding            # noqa: E402
from diffusion import DiffusionProcess             # noqa: E402
from config import DIFFUSION_CONFIG                # noqa: E402

DATA = os.path.join(HERE, 'data')
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

# channels transformed with log1p (counts); the rest are rates/shares in [0,1]
COUNT_CHANNELS = {'passes', 'shots', 'shots_on_target', 'goals', 'corners',
                  'freekicks', 'offsides', 'turnovers_won', 'tackles', 'red_cards',
                  'yellow_cards', 'xg', 'sprints', 'sequences'}
RATE_CHANNELS = {'poss_share', 'pass_comp_rate', 'field_tilt', 'save_pct'}
# match-level positive-continuous aggregates: log1p-transformed like a count (variance
# stabilization) but reconstructed as a LEVEL (mean over segments), never a sum.
LEVEL_LOG_CHANNELS = {'recovery_sec'}
LOGIT_CLIP = 1e-3


def load_corpus():
    X = np.load(os.path.join(DATA, 'segment_tensor.npy'))          # (n, seg, 2, C)
    meta = json.load(open(os.path.join(DATA, 'conditions.json')))
    return X, meta


def cond_vector(cond: dict) -> np.ndarray:
    """Tactics (5 per side) + the 5 tactic GAPS. Engine-internal signals only:
    the brain compiles tactics from ITS OWN ratings, which disagree with any external
    strength table (e.g. its narratives rate Belgium~Senegal near-equal, and some sims
    rate Cape Verde above Argentina) — conditioning on an external table injects
    contradictory information and was the dominant LOFO failure mode."""
    th, ta = cond['tactics_home'], cond['tactics_away']
    gaps = [h - a for h, a in zip(th, ta)]
    return np.array(th + ta + gaps, dtype=np.float64)


def flip_cond_vector(c: np.ndarray) -> np.ndarray:
    """Swap home/away roles in the condition vector."""
    out = np.empty_like(c)
    out[0:5], out[5:10] = c[5:10], c[0:5]
    out[10:15] = -c[10:15]
    return out


def transform(X: np.ndarray, channels) -> np.ndarray:
    """(…, C) raw tensor -> variance-stabilized: log1p counts, logit rates."""
    Z = X.astype(np.float64).copy()
    for i, ch in enumerate(channels):
        v = Z[..., i]
        if ch in COUNT_CHANNELS or ch in LEVEL_LOG_CHANNELS:
            Z[..., i] = np.log1p(v)
        else:
            p = np.clip(v, LOGIT_CLIP, 1 - LOGIT_CLIP)
            Z[..., i] = np.log(p / (1 - p))
    return Z


def inverse_transform(Z: np.ndarray, channels) -> np.ndarray:
    X = Z.astype(np.float64).copy()
    for i, ch in enumerate(channels):
        v = X[..., i]
        if ch in COUNT_CHANNELS or ch in LEVEL_LOG_CHANNELS:
            X[..., i] = np.maximum(np.expm1(v), 0.0)
        else:
            X[..., i] = 1.0 / (1.0 + np.exp(-v))
    return X


def flip_tensor(Z: np.ndarray, channels) -> np.ndarray:
    """Swap the side axis. All channels (incl. field_tilt, defined as center-distance)
    are label-neutral, so a pure side swap is an exact symmetry of the engine."""
    return Z[:, :, ::-1, :].copy()


# ── Confirmatory factor structure (arch='cfa') ───────────────────────────────
# Instead of exploratory PCA factors (whatever explains variance — mixtures), impose
# football-meaningful factor BLOCKS: each factor loads only on a domain group of
# channels, so the k latent drivers are interpretable (tempo / dominance / finishing /
# physicality / press / control). 'sym' = both sides load with the same sign (a shared
# match property); 'asym' = home +, away − (a one-sided property like territorial
# dominance, which flips sign correctly under the home/away augmentation).
CFA_FACTORS = [
    ('tempo',       'sym',  {'shots': 1, 'shots_on_target': 1, 'corners': 1,
                             'freekicks': 1, 'passes': 1, 'sequences': 1, 'sprints': 1}),
    ('dominance',   'asym', {'poss_share': 1, 'field_tilt': 1, 'shots': 1,
                             'corners': 1, 'xg': 1}),
    ('finishing',   'sym',  {'goals': 1, 'xg': 1, 'shots_on_target': 1}),
    ('physicality', 'sym',  {'freekicks': 1, 'tackles': 1, 'offsides': 1,
                             'yellow_cards': 1, 'red_cards': 1}),
    ('press',       'sym',  {'turnovers_won': 1, 'sprints': 1, 'recovery_sec': -1,
                             'sequences': 1}),
    ('control',     'sym',  {'passes': 1, 'pass_comp_rate': 1, 'poss_share': 1}),
]


def build_cfa_beta(channels, n_seg):
    """(beta (d,k), mask (d,k)) for the confirmatory factor blocks. d = n_seg·2·C, in the
    same segment-major / side / channel flatten order as build_training_arrays."""
    C = len(channels)
    d = n_seg * 2 * C
    k = len(CFA_FACTORS)
    beta = np.zeros((d, k), dtype=np.float64)
    mask = np.zeros((d, k), dtype=np.float64)
    cidx = {c: j for j, c in enumerate(channels)}
    for f, (_, mode, loads) in enumerate(CFA_FACTORS):
        for ch, sgn in loads.items():
            if ch not in cidx:
                continue
            j = cidx[ch]
            for seg in range(n_seg):
                for side in (0, 1):
                    dim = seg * (2 * C) + side * C + j
                    s = sgn if (mode == 'sym' or side == 0) else -sgn
                    beta[dim, f] = s
                    mask[dim, f] = 1.0
        nrm = np.linalg.norm(beta[:, f])
        if nrm > 0:
            beta[:, f] /= nrm
    return beta, mask


CFA_NAMES = [n for n, _, _ in CFA_FACTORS]


def build_training_arrays(X, meta, exclude_matchups=()):
    """-> (Z_flat (2n', d), C_cond (2n', 13), scaler dict, kept sim indices)."""
    channels = meta['channels']
    keep = [i for i, c in enumerate(meta['conds'])
            if not any(m in c['sim_dir'] for m in exclude_matchups)]
    Z = transform(X[keep], channels)
    C = np.stack([cond_vector(meta['conds'][i]) for i in keep])
    Zf = flip_tensor(Z, channels)
    Cf = np.stack([flip_cond_vector(c) for c in C])
    Zall = np.concatenate([Z, Zf]).reshape(len(keep) * 2, -1)
    Call = np.concatenate([C, Cf])
    mu, sd = Zall.mean(0), Zall.std(0) + 1e-8
    cmu, csd = Call.mean(0), Call.std(0) + 1e-8
    scaler = {'mu': mu, 'sd': sd, 'cmu': cmu, 'csd': csd,
              'channels': channels, 'segments': meta['segments']}
    return (Zall - mu) / sd, (Call - cmu) / csd, scaler, keep


class CondFactorScoreNet(nn.Module):
    """Paper's factor score with the condition vector fed into the subspace net."""

    def __init__(self, data_dim, cond_dim, factor_dim=6, hidden=128, n_layers=3,
                 time_embed_dim=64, init_beta=None, init_sigma_diag=None,
                 orthonormalize=True):
        super().__init__()
        self.data_dim, self.factor_dim = data_dim, factor_dim
        self.time_embed = TimeEmbedding(time_embed_dim)
        if init_sigma_diag is not None:
            self.log_c = nn.Parameter(torch.log(
                torch.as_tensor(init_sigma_diag, dtype=torch.float32).clamp(min=1e-6)))
        else:
            self.log_c = nn.Parameter(torch.zeros(data_dim))
        if init_beta is not None:
            B = torch.as_tensor(init_beta, dtype=torch.float32)
            if orthonormalize:
                V, _ = torch.linalg.qr(B)   # exploratory: orthonormal PCA-style factors
                V = V[:, :factor_dim]
            else:
                V = B[:, :factor_dim]       # confirmatory: keep the block structure intact
        else:
            V, _ = torch.linalg.qr(torch.randn(data_dim, factor_dim))
        self.V = nn.Parameter(V)

        layers, indim = [], factor_dim + time_embed_dim + cond_dim
        for _ in range(n_layers):
            layers += [nn.Linear(indim, hidden), nn.SiLU()]
            indim = hidden
        layers += [nn.Linear(hidden, factor_dim)]
        self.g = nn.Sequential(*layers)

    def forward(self, x, t, c):
        if t.dim() == 0:
            t = t.expand(x.shape[0])
        alpha = torch.exp(-0.5 * t).unsqueeze(-1)
        h = (1 - alpha ** 2).clamp(min=1e-6)
        d_t = 1.0 / (h + (alpha ** 2) * torch.exp(self.log_c.clamp(max=4.0)))
        dtx = x * d_t
        z = dtx @ self.V
        emb = self.time_embed(t)
        out = self.g(torch.cat([z, emb, c], dim=1))
        subspace = alpha * (out @ self.V.T) * d_t
        return subspace - dtx


class PlainCondScoreNet(nn.Module):
    """Ablation arm: a direct conditional MLP score s(x,t,c) with NO factor
    structure — the 'what if we hadn't borrowed the DFM architecture' baseline."""

    def __init__(self, data_dim, cond_dim, hidden=256, n_layers=3, time_embed_dim=64):
        super().__init__()
        self.time_embed = TimeEmbedding(time_embed_dim)
        layers, indim = [], data_dim + time_embed_dim + cond_dim
        for _ in range(n_layers):
            layers += [nn.Linear(indim, hidden), nn.SiLU()]
            indim = hidden
        layers += [nn.Linear(hidden, data_dim)]
        self.g = nn.Sequential(*layers)

    def forward(self, x, t, c):
        if t.dim() == 0:
            t = t.expand(x.shape[0])
        emb = self.time_embed(t)
        return self.g(torch.cat([x, emb, c], dim=1))


def train_conditional(Z, C, factor_dim=6, epochs=4000, lr=1e-3, batch=64, seed=0,
                      verbose=True, arch='factor', channels=None, n_seg=None,
                      cfa_penalty=0.05):
    """h-weighted denoising score matching (predict-noise parametrization) —
    the parent repo's unweighted score MSE is dominated by t→t0 blow-up.
    arch: 'factor' (exploratory PCA Lemma-1), 'cfa' (confirmatory football factor
    blocks — interpretable, off-block loadings penalized), 'plain' (MLP ablation)."""
    torch.manual_seed(seed)
    n, d = Z.shape
    t0, T = DIFFUSION_CONFIG['t0'], DIFFUSION_CONFIG['T']

    off_block = None
    if arch == 'plain':
        model = PlainCondScoreNet(d, C.shape[1]).to(DEVICE)
    elif arch in ('cfa', 'cfa_hybrid'):
        # confirmatory: block-structured loadings kept near the football-semantic blocks.
        # 'cfa_hybrid' (recommended) appends k_extra FREE factors that PCA the residual the
        # blocks cannot span (cross-block covariance the hard blocks forbid) — interpretable
        # named factors + fidelity. The off-block penalty applies ONLY to the semantic cols.
        beta0, mask = build_cfa_beta(channels, n_seg)
        k_sem = beta0.shape[1]
        # residual after removing the semantic subspace, then extra free factors on it
        proj = beta0 @ np.linalg.pinv(beta0.T @ beta0) @ beta0.T
        resid = Z - Z @ proj
        k_extra = max(0, factor_dim - k_sem) if arch == 'cfa_hybrid' else 0
        if k_extra:
            evals, evecs = np.linalg.eigh(np.cov(resid, rowvar=False))
            extra = evecs[:, ::-1][:, :k_extra].copy()
            beta0 = np.concatenate([beta0, extra], axis=1)
            mask = np.concatenate([mask, np.ones((d, k_extra))], axis=1)   # no penalty on extras
        r2 = Z - (Z @ beta0) @ np.linalg.pinv(beta0.T @ beta0) @ beta0.T
        sigma0 = r2.var(0) + 1e-4
        k_cfa = beta0.shape[1]
        model = CondFactorScoreNet(d, C.shape[1], factor_dim=k_cfa,
                                   init_beta=beta0, init_sigma_diag=sigma0,
                                   orthonormalize=False).to(DEVICE)
        off_block = torch.tensor(1.0 - mask[:, :k_cfa], dtype=torch.float32, device=DEVICE)
    else:
        # PCA warm start on the (already standardized) training matrix
        cov = np.cov(Z, rowvar=False)
        evals, evecs = np.linalg.eigh(cov)
        beta0 = evecs[:, ::-1][:, :factor_dim].copy()
        resid = Z - (Z @ beta0) @ beta0.T
        sigma0 = resid.var(0) + 1e-4
        model = CondFactorScoreNet(d, C.shape[1], factor_dim=factor_dim,
                                   init_beta=beta0, init_sigma_diag=sigma0).to(DEVICE)
    Zt = torch.tensor(Z, dtype=torch.float32, device=DEVICE)
    Ct = torch.tensor(C, dtype=torch.float32, device=DEVICE)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

    model.train()
    for ep in range(epochs):
        idx = torch.randint(0, n, (min(batch, n),), device=DEVICE)
        x0, c = Zt[idx], Ct[idx]
        t = t0 + (T - t0) * torch.rand(len(idx), device=DEVICE)
        alpha = torch.exp(-0.5 * t).unsqueeze(-1)
        h = (1 - alpha ** 2).clamp(min=1e-6)
        eps = torch.randn_like(x0)
        xt = alpha * x0 + h.sqrt() * eps
        pred = model(xt, t, c)
        loss = ((h.sqrt() * pred + eps) ** 2).mean()      # h-weighted score MSE
        if off_block is not None:                         # keep V near the semantic blocks
            loss = loss + cfa_penalty * (model.V * off_block).pow(2).sum()
        opt.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        if verbose and (ep + 1) % max(1, epochs // 8) == 0:
            print(f'  [train] epoch {ep + 1}/{epochs} loss {loss.item():.4f}')
    model.eval()
    return model
