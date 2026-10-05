"""Models for W11: LightGBM and GRU (retrained daily on the rolling recordings) and Chronos-Bolt
(zero-shot, loaded read-only from the local HF cache). A model directory is immutable once
written; MODELS/latest.json names the one in use."""
from __future__ import annotations
import json, os, time
from pathlib import Path
import numpy as np, pandas as pd
from .config import MODELS, COINS, STEP, BINS, CHRONOS_MODEL
from .features import ALL, channels, static

LGBM_PARAMS = dict(n_estimators=600, learning_rate=0.02, num_leaves=15, min_child_samples=80, subsample=0.8, subsample_freq=1,
                   colsample_bytree=0.7, reg_lambda=10.0, num_threads=4, verbose=-1)
KMAX = max(BINS.values())


# ───────────── GRU (the module validated in the 2026-10-03 study) ─────────────
def _torch():
    import torch, torch.nn as nn
    torch.set_num_threads(2)
    class Net(nn.Module):
        def __init__(s, nc, ns, h=32):
            super().__init__(); s.gru = nn.GRU(nc, h, batch_first=True); s.head = nn.Sequential(nn.Linear(h + ns, 32), nn.ReLU(), nn.Dropout(0.2), nn.Linear(32, 1))
        def forward(s, seq, mask, st):
            out, _ = s.gru(seq); return s.head(torch.cat([out[:, -1], st], 1)).squeeze(1)
    return torch, nn, Net


def gru_batch(C, df, wi_col="wi"):
    seq = np.zeros((len(df), KMAX, C.shape[2]), np.float32); mask = np.zeros((len(df), KMAX), np.float32)
    for j, (wi, k) in enumerate(zip(df[wi_col].values, df.k.values)):
        k = int(min(k, KMAX)); seq[j, KMAX - k:] = C[wi, :k]; mask[j, KMAX - k:] = 1
    return seq, mask, static(df)


def train_gru(C, FR, train_days, val_day, seed=0, max_epochs=40, patience=6, no_price=False):
    torch, nn, Net = _torch(); torch.manual_seed(seed); np.random.seed(seed)
    tr, va = FR[FR.day.isin(train_days)], FR[FR.day == val_day]
    s_tr, m_tr, st_tr = gru_batch(C, tr); s_va, m_va, st_va = gru_batch(C, va)
    if no_price: s_tr, st_tr = strip_price(s_tr, st_tr); s_va, st_va = strip_price(s_va, st_va)
    mu = s_tr.reshape(-1, C.shape[2]).mean(0); sd = s_tr.reshape(-1, C.shape[2]).std(0) + 1e-6
    norm = lambda s, m: ((s - mu) / sd * m[:, :, None]).astype(np.float32)
    Xtr, Xva = norm(s_tr, m_tr), norm(s_va, m_va)
    net = Net(C.shape[2], st_tr.shape[1]); opt = torch.optim.Adam(net.parameters(), lr=2e-3, weight_decay=1e-4); lossf = nn.BCEWithLogitsLoss()
    T = lambda a: torch.tensor(a)
    ytr, yva = T(tr.y_yes.values.astype(np.float32)), va.y_yes.values.astype(np.float32)
    best, best_state, bad, n = 9.0, None, 0, len(tr); idx = np.arange(n); ep_used = 0
    for ep in range(max_epochs):
        net.train(); np.random.shuffle(idx)
        for a in range(0, n, 256):
            b = idx[a:a + 256]; opt.zero_grad(); loss = lossf(net(T(Xtr[b]), T(m_tr[b]), T(st_tr[b])), ytr[b]); loss.backward(); opt.step()
        net.eval()
        with torch.no_grad(): pv = torch.sigmoid(net(T(Xva), T(m_va), T(st_va))).numpy()
        l = float(-np.mean(yva * np.log(np.clip(pv, 1e-4, 1)) + (1 - yva) * np.log(np.clip(1 - pv, 1e-4, 1))))
        ep_used = ep + 1
        if l < best - 1e-4: best, best_state, bad = l, {k: v.clone() for k, v in net.state_dict().items()}, 0
        else: bad += 1
        if bad >= patience: break
    net.load_state_dict(best_state)
    return net, dict(mu=mu.tolist(), sd=sd.tolist(), val_logloss=best, epochs=ep_used, nc=C.shape[2], ns=st_tr.shape[1])


def strip_price(seq, st):
    """The no-price GRU: zero the Kalshi channel (7) and the kyes static (1)."""
    seq = seq.copy(); st = st.copy(); seq[:, :, 7] = 0.0; st[:, 1] = 0.0; return seq, st


class GRUPredictor:
    def __init__(self, path: Path, meta: dict, no_price: bool = False):
        torch, nn, Net = _torch(); self.torch = torch; self.no_price = no_price
        self.net = Net(meta["nc"], meta["ns"]); self.net.load_state_dict(torch.load(path, map_location="cpu")); self.net.eval()
        self.mu = np.array(meta["mu"], np.float32); self.sd = np.array(meta["sd"], np.float32)
    def predict(self, C, df):
        s, m, st = gru_batch(C, df)
        if self.no_price: s, st = strip_price(s, st)
        s = ((s - self.mu) / self.sd * m[:, :, None]).astype(np.float32)
        with self.torch.no_grad(): return self.torch.sigmoid(self.net(self.torch.tensor(s), self.torch.tensor(m), self.torch.tensor(st))).numpy()


# ───────────── Chronos-Bolt zero-shot ─────────────
class ChronosPredictor:
    QL = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
    def __init__(self, name=CHRONOS_MODEL):
        os.environ.setdefault("HF_HUB_OFFLINE", "1"); os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import torch
        from chronos import BaseChronosPipeline
        torch.set_num_threads(2)
        self.torch = torch; self.pipe = BaseChronosPipeline.from_pretrained(name, device_map="cpu", torch_dtype=torch.float32); self.name = name
    def predict(self, contexts: list, horizon_steps: int, strikes: list, with_dir: bool = False):
        """P(settlement >= strike): normal fit to the quantiles of the mean of the last 6 forecast
        points (the 60-s settlement average). Context/horizon at 10-second steps.
        with_dir=True also returns the forecast median minus the current index in bp (direction)."""
        from scipy.stats import norm
        ctx = [self.torch.tensor(np.asarray(c, dtype=np.float32)) for c in contexts]
        H = max(1, min(64, int(horizon_steps)))
        q, _ = self.pipe.predict_quantiles(ctx, prediction_length=H, quantile_levels=self.QL); q = q.numpy()
        out = np.full(len(contexts), np.nan); dirn = np.full(len(contexts), np.nan)
        for j, k in enumerate(strikes):
            avg = q[j, -min(6, H):].mean(0); med, iqr = avg[4], avg[6] - avg[2]; sig = max(iqr / 1.349, 1e-9)
            out[j] = 1 - norm.cdf((k - med) / sig); cur = float(contexts[j][-1])
            dirn[j] = (np.log(med / cur) * 1e4) if cur > 0 and med > 0 else np.nan
        return (out, dirn) if with_dir else out


# ───────────── registry ─────────────
def latest_stamp() -> str | None:
    p = MODELS / "latest.json"
    try: return json.loads(p.read_text())["stamp"]
    except (OSError, ValueError, KeyError): return None


def load_models(stamp: str | None = None):
    """{'stamp', 'lgbm', 'meta'} for the observer's main process (LightGBM only: torch models live
    in model_worker, a separate process - see there for why). None when nothing is trained yet."""
    import lightgbm as lgb
    stamp = stamp or latest_stamp()
    if not stamp: return None
    d = MODELS / stamp; meta = json.loads((d / "meta.json").read_text())
    out = {"stamp": stamp, "lgbm": lgb.Booster(model_file=str(d / "lgbm.txt")), "meta": meta, "has_gru": (d / "gru.pt").exists()}
    for nm in ("lgbm_np", "lgbm_dir", "lgbm_mag"):            # no-price binary, remaining-move direction, remaining-move magnitude
        out[nm] = lgb.Booster(model_file=str(d / f"{nm}.txt")) if (d / f"{nm}.txt").exists() else None
    return out


def lgbm_predict(booster, F: pd.DataFrame, cols=None) -> np.ndarray:
    from .features import ALL as _ALL
    return booster.predict(F[cols or _ALL].values.astype(float))


def logistic_predict(coef: dict, F: pd.DataFrame) -> np.ndarray:
    """p = sigmoid(b0 + sum(b_i x_i)) with the training-time coefficients (features.logit_cols design)."""
    from .features import logit_cols
    X = logit_cols(F, coef["cols"]); zlin = coef["intercept"] + X @ np.asarray(coef["coef"], float)
    out = 1 / (1 + np.exp(-zlin)); out[~np.isfinite(zlin)] = np.nan; return out
