"""GRU training in its OWN process (torch only): LightGBM/sklearn and torch each ship a libomp on
macOS and deadlock when loaded together, so train.py runs this as a subprocess.
    python -m crypto_trading.crypto_strategies.w13_maker_mirror.gru_train <stamp>"""
from __future__ import annotations
import json, sys
import numpy as np, pandas as pd
from .config import DATA, MODELS, COINS
from .features import channels
from .models import train_gru


def main(argv=None) -> int:
    args = argv or sys.argv[1:]; stamp = args[0]; no_price = "--no-price" in args
    dd, out = DATA / stamp, MODELS / stamp
    X = np.concatenate([np.load(dd / f"seq_{c}.npy") for c in COINS]); M = pd.concat([pd.read_parquet(dd / f"seq_{c}_meta.parquet") for c in COINS], ignore_index=True)
    FR = pd.read_parquet(dd / "FR.parquet")
    key = {(c, t): i for i, (c, t) in enumerate(zip(M.coin, M.close_ts))}; FR["wi"] = [key[(c, t)] for c, t in zip(FR.coin, FR.close_ts)]
    days = sorted(FR.day.unique()); counts = FR.groupby("day").size()
    full = [d for d in days if counts[d] >= 300] or days
    val_day = full[-1]; train_days = [d for d in days if d != val_day]
    C, vol_median = channels(X, M)
    net, gmeta = train_gru(C, FR, train_days, val_day, no_price=no_price)
    import torch; out.mkdir(parents=True, exist_ok=True); tag = "gru_np" if no_price else "gru"
    torch.save(net.state_dict(), out / f"{tag}.pt")
    gmeta["vol_median"] = float(vol_median); gmeta["no_price"] = no_price; (out / f"{tag}_meta.json").write_text(json.dumps(gmeta))
    print(json.dumps({tag: {k: gmeta[k] for k in ("val_logloss", "epochs")}})); return 0


if __name__ == "__main__":
    raise SystemExit(main())
