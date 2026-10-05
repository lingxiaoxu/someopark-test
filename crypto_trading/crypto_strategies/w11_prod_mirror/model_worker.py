"""Torch-only inference worker (GRU + Chronos) for the W11 observer. Kept in its own process
because torch's libomp deadlocks with LightGBM's in one process on macOS, and so that a slow
forecast can never delay the observer: the observer waits a bounded time, else records NaN.

Protocol (one JSON object per line on stdin/stdout):
  {"cmd": "reload", "stamp": "<stamp>"}              -> {"ok": true, "stamp": ..., "gru": bool, "chronos": bool}
  {"cmd": "predict", "id": n, "npz": "<path>", "k": k, "horizon": H} -> {"ok": true, "id": n, "p_gru": [...], "p_chronos": [...]}
The npz holds X [n,K,19], meta columns (coin, close_ts, strike, rv60_prior, rv15_prior, hour, dow, dev8h) and the
Chronos contexts [n, L] with strikes."""
from __future__ import annotations
import json, sys, traceback
import numpy as np, pandas as pd
from .config import MODELS, COINS, CHRONOS_MODEL
from .features import feats, channels
from .models import GRUPredictor, ChronosPredictor


def main() -> int:
    gru = None; gru_np = None; chronos = None; chronos_base = None; stamp = None; vol_median = None
    try: chronos = ChronosPredictor(CHRONOS_MODEL)
    except Exception as e: print(json.dumps({"event": "chronos_unavailable", "err": str(e)[:200]}), flush=True)   # noqa: BLE001
    try: chronos_base = ChronosPredictor("amazon/chronos-bolt-base")
    except Exception as e: print(json.dumps({"event": "chronos_base_unavailable", "err": str(e)[:200]}), flush=True)   # noqa: BLE001
    for line in sys.stdin:
        try:
            req = json.loads(line)
            if req.get("cmd") == "reload":
                stamp = req["stamp"]; d = MODELS / stamp
                gru = gru_np = None
                if (d / "gru.pt").exists():
                    gmeta = json.loads((d / "gru_meta.json").read_text()); gru = GRUPredictor(d / "gru.pt", gmeta); vol_median = gmeta.get("vol_median")
                if (d / "gru_np.pt").exists():
                    gmeta2 = json.loads((d / "gru_np_meta.json").read_text()); gru_np = GRUPredictor(d / "gru_np.pt", gmeta2, no_price=True); vol_median = vol_median or gmeta2.get("vol_median")
                print(json.dumps({"ok": True, "stamp": stamp, "gru": gru is not None, "gru_np": gru_np is not None, "chronos": chronos is not None, "chronos_base": chronos_base is not None}), flush=True); continue
            if req.get("cmd") == "predict":
                z = np.load(req["npz"], allow_pickle=True); X = z["X"]; k = int(req["k"])
                M = pd.DataFrame({c: z[c] for c in ("coin", "close_ts", "strike", "rv60_prior", "rv15_prior", "hour", "dow", "dev8h")})
                n = len(X); p_g = [None] * n; p_gn = [None] * n; p_c = [None] * n; p_cb = [None] * n; c_dir = [None] * n
                if gru is not None or gru_np is not None:
                    F = feats(X, M, k); F["wi"] = np.arange(n); C, _ = channels(X, M, vol_median)
                    if gru is not None: p_g = [float(v) for v in gru.predict(C, F)]
                    if gru_np is not None: p_gn = [float(v) for v in gru_np.predict(C, F)]
                if "ctx" in z:
                    ctx = z["ctx"]; strikes = z["strike"]; ok = [i for i in range(n) if np.isfinite(ctx[i]).all() and np.isfinite(strikes[i])]
                    if ok:
                        if chronos is not None:
                            pc, dv = chronos.predict([ctx[i] for i in ok], int(req["horizon"]), [float(strikes[i]) for i in ok], with_dir=True)
                            for j, i in enumerate(ok): p_c[i] = float(pc[j]); c_dir[i] = None if not np.isfinite(dv[j]) else float(dv[j])
                        if chronos_base is not None:
                            pb = chronos_base.predict([ctx[i] for i in ok], int(req["horizon"]), [float(strikes[i]) for i in ok])
                            for j, i in enumerate(ok): p_cb[i] = float(pb[j])
                print(json.dumps({"ok": True, "id": req.get("id"), "p_gru": p_g, "p_gru_np": p_gn, "p_chronos": p_c, "p_chronos_base": p_cb, "chronos_dir_bp": c_dir}), flush=True); continue
            if req.get("cmd") == "quit": break
        except Exception as e:                                                       # noqa: BLE001
            print(json.dumps({"ok": False, "id": req.get("id") if isinstance(req, dict) else None, "err": str(e)[:300], "tb": traceback.format_exc()[-300:]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
