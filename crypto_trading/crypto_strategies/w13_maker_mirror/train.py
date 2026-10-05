"""Daily retraining for W11: rebuild the rolling dataset from the recordings (read-only),
train LightGBM + GRU, validate on the last full day, write MODELS/<stamp>/ and DATA/<stamp>/,
then point MODELS/latest.json at the new stamp. Nothing outside OUT is written.

    python -m crypto_trading.crypto_strategies.w13_maker_mirror.train [--days 14]
"""
from __future__ import annotations
import argparse, json, shutil, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import numpy as np, pandas as pd
from .config import COINS, OUT, MODELS, DATA, TRAIN_DAYS, KEEP_STAMPS, STRIPS, HL, INDEX, DERIBIT, BINS
from .features import (feats, ALL, NOPRICE, BLEND1, BLEND2, logit_cols, load_jsonl, arr, window_array, prior_vols, IDX_FN, CTX_FN, TRD_FN, BOOK_FN, STRIP_FN, DV_FN)
from .models import LGBM_PARAMS


def build_dataset(ndays: int, end_close: float) -> tuple[np.ndarray, pd.DataFrame]:
    from crypto_trading.crypto_strategies.w7_scenarios import inputs, funding as fundmod
    days = [(datetime.fromtimestamp(end_close, timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(ndays + 1, -1, -1)]
    fund = fundmod.load(); Xs, Ms = [], []
    for c in COINS:
        I = arr(sum((load_jsonl(INDEX / c / f"{d}.jsonl", IDX_FN) for d in days), []), 2)
        C = arr(sum((load_jsonl(HL / "context" / c / f"{d}.jsonl", CTX_FN) for d in days), []), 7)
        T = arr(sum((load_jsonl(HL / "trades" / c / f"{d}.jsonl", TRD_FN) for d in days), []), 3)
        B = arr(sum((load_jsonl(HL / "book" / c / f"{d}.jsonl", BOOK_FN) for d in days), []), 7)
        S_ = sum((load_jsonl(STRIPS / f"KX{c}15M" / "orderbook" / f"{d}.jsonl", STRIP_FN) for d in days), [])
        D = arr(sum((load_jsonl(DERIBIT / "dvol" / c / f"{d}.jsonl", DV_FN) for d in days), []), 2) if c in ("BTC", "ETH") else np.zeros((0, 2))
        S = arr(S_, 4)
        strikes = {}
        for t, px, ct, k in S_: strikes.setdefault(int(ct), float(k))
        closes = sorted(ct for ct in strikes if ct + 900 in strikes and end_close - ndays * 86400 < ct <= end_close)
        meta, X = [], []
        for ct in closes:
            Sx = S[S[:, 2] == ct]
            X.append(window_array(ct, I, C, T, B, Sx, D)); rv60, rv15 = prior_vols(I, ct - 900)
            meta.append(dict(coin=c, close_ts=ct, strike=strikes[ct], strike_next=strikes[ct + 900], rv60_prior=rv60, rv15_prior=rv15,
                             hour=datetime.fromtimestamp(ct, timezone.utc).hour, dow=datetime.fromtimestamp(ct, timezone.utc).weekday()))
        M = pd.DataFrame(meta); M["dev8h"] = inputs.funding_at_open(M, fund).values if len(M) else []
        Xs.append(np.stack(X) if X else np.zeros((0, 180, 19), np.float32)); Ms.append(M)
    return np.concatenate(Xs), pd.concat(Ms, ignore_index=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__); ap.add_argument("--days", type=int, default=TRAIN_DAYS); ap.add_argument("--no-gru", action="store_true")
    a = ap.parse_args(argv)
    import lightgbm as lgb
    from sklearn.metrics import roc_auc_score, log_loss
    t0 = time.time(); now = time.time(); end_close = (now - 20 * 60) // 900 * 900
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    X, M = build_dataset(a.days, end_close)
    FR = pd.concat([feats(X, M, k).assign(bin=b) for b, k in BINS.items()], ignore_index=True)
    key = {(c, t): i for i, (c, t) in enumerate(zip(M.coin, M.close_ts))}; FR["wi"] = [key[(c, t)] for c, t in zip(FR.coin, FR.close_ts)]
    FR = FR[FR.y_yes.notna()].reset_index(drop=True)
    days = sorted(FR.day.unique()); counts = FR.groupby("day").size()
    full = [d for d in days if counts[d] >= 300] or days          # validate on the last FULL day (today is partial)
    val_day = full[-1]; train_days = [d for d in days if d != val_day]
    tr, va = FR[FR.day.isin(train_days)], FR[FR.day == val_day]
    ll = lambda y, p: float(log_loss(y, np.clip(p, 1e-4, 1 - 1e-4), labels=[0, 1]))
    # LightGBM (validation = last day, then refit on everything for the model that goes live)
    m = lgb.LGBMClassifier(**LGBM_PARAMS).fit(tr[ALL], tr.y_yes); pv = m.predict_proba(va[ALL])[:, 1]
    val = {"lgbm_auc": float(roc_auc_score(va.y_yes, pv)), "lgbm_logloss": ll(va.y_yes, pv),
           "market_auc": float(roc_auc_score(va.y_yes[va.kyes.notna()], va.kyes[va.kyes.notna()])), "market_logloss": ll(va.y_yes[va.kyes.notna()], va.kyes[va.kyes.notna()]),
           "fair_auc": float(roc_auc_score(va.y_yes[va.p_fair_prior.notna()], va.p_fair_prior[va.p_fair_prior.notna()]))}
    full = lgb.LGBMClassifier(**LGBM_PARAMS).fit(FR[ALL], FR.y_yes)
    out = MODELS / stamp; out.mkdir(parents=True, exist_ok=True); full.booster_.save_model(str(out / "lgbm.txt"))
    # the other tested models (all validated on the last full day, then refit on everything)
    from sklearn.linear_model import LogisticRegression
    from scipy.stats import spearmanr
    m_np = lgb.LGBMClassifier(**LGBM_PARAMS).fit(tr[NOPRICE], tr.y_yes); p_ = m_np.predict_proba(va[NOPRICE])[:, 1]
    val["lgbm_np_auc"] = float(roc_auc_score(va.y_yes, p_)); val["lgbm_np_logloss"] = ll(va.y_yes, p_)
    lgb.LGBMClassifier(**LGBM_PARAMS).fit(FR[NOPRICE], FR.y_yes).booster_.save_model(str(out / "lgbm_np.txt"))
    trd, vad = tr[tr.y_dir.notna()], va[va.y_dir.notna()]
    m_dir = lgb.LGBMClassifier(**LGBM_PARAMS).fit(trd[NOPRICE], trd.y_dir); p_ = m_dir.predict_proba(vad[NOPRICE])[:, 1]
    val["dir_auc"] = float(roc_auc_score(vad.y_dir, p_))
    lgb.LGBMClassifier(**LGBM_PARAMS).fit(FR[FR.y_dir.notna()][NOPRICE], FR[FR.y_dir.notna()].y_dir).booster_.save_model(str(out / "lgbm_dir.txt"))
    reg = {**LGBM_PARAMS}; m_mag = lgb.LGBMRegressor(**reg).fit(tr[NOPRICE], tr.y_rem); p_ = m_mag.predict(va[NOPRICE])
    val["mag_spearman"] = float(spearmanr(va.y_rem, p_).correlation); val["mag_r2"] = float(1 - np.sum((va.y_rem - p_) ** 2) / np.sum((va.y_rem - tr.y_rem.mean()) ** 2))
    lgb.LGBMRegressor(**reg).fit(FR[NOPRICE], FR.y_rem).booster_.save_model(str(out / "lgbm_mag.txt"))
    blends = {}
    for nm, cols in (("blend1", BLEND1), ("blend2", BLEND2)):
        Xtr, Xva, Xall = logit_cols(tr, cols), logit_cols(va, cols), logit_cols(FR, cols)
        mtr, mva, mall = np.isfinite(Xtr).all(1), np.isfinite(Xva).all(1), np.isfinite(Xall).all(1)
        lr = LogisticRegression(max_iter=3000).fit(Xtr[mtr], tr.y_yes.values[mtr]); p_ = lr.predict_proba(Xva[mva])[:, 1]
        val[f"{nm}_auc"] = float(roc_auc_score(va.y_yes.values[mva], p_)); val[f"{nm}_logloss"] = ll(va.y_yes.values[mva], p_)
        lr = LogisticRegression(max_iter=3000).fit(Xall[mall], FR.y_yes.values[mall])
        blends[nm] = {"cols": cols, "coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])}
    zq = FR.z[np.isfinite(FR.z)].quantile([1 / 3, 2 / 3]).tolist()
    meta = {"stamp": stamp, "train_days": [str(d) for d in days], "val_day": str(val_day), "entries": int(len(FR)), "windows": int(M.close_ts.nunique()),
            "end_close_utc": datetime.fromtimestamp(end_close, timezone.utc).isoformat(), "features": ALL, "validation": val, "lgbm_params": LGBM_PARAMS,
            "blends": blends, "z_terciles": zq}
    dd = DATA / stamp; dd.mkdir(parents=True, exist_ok=True)
    for c in COINS:
        mc = M.coin.values == c; np.save(dd / f"seq_{c}.npy", X[mc]); M[mc].to_parquet(dd / f"seq_{c}_meta.parquet", index=False)
    FR.to_parquet(dd / "FR.parquet", index=False)
    if not a.no_gru:
        # torch in its own process: its libomp deadlocks with LightGBM's when both are loaded here
        import subprocess, os
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[3]), OMP_NUM_THREADS="2")
        for tag, extra in (("gru", []), ("gru_np", ["--no-price"])):
            r = subprocess.run([sys.executable, "-m", "crypto_trading.crypto_strategies.w13_maker_mirror.gru_train", stamp] + extra, env=env, capture_output=True, text=True, timeout=2400)
            if r.returncode == 0 and (out / f"{tag}_meta.json").exists():
                meta[tag] = json.loads((out / f"{tag}_meta.json").read_text()); val[f"{tag}_val_logloss"] = meta[tag]["val_logloss"]
            else:
                meta[f"{tag}_error"] = (r.stderr or r.stdout)[-500:]
    meta["seconds"] = round(time.time() - t0, 1); (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    tmp = MODELS / "latest.json.tmp"; tmp.write_text(json.dumps({"stamp": stamp, "at": datetime.now(timezone.utc).isoformat()})); tmp.replace(MODELS / "latest.json")
    for root in (MODELS, DATA):      # keep the last KEEP_STAMPS snapshots
        stamps = sorted(p.name for p in root.iterdir() if p.is_dir())
        for s_ in stamps[:-KEEP_STAMPS]: shutil.rmtree(root / s_, ignore_errors=True)
    print(json.dumps({"stamp": stamp, "entries": meta["entries"], "windows": meta["windows"], "validation": val, "seconds": meta["seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
