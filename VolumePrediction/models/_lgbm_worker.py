"""LightGBM 子进程 worker(进程隔离方案的执行端;绝不 import torch)。

用法: python _lgbm_worker.py <payload.json>
payload: {op: fit|predict, x, [y], booster, [out], [n_estimators], [random_state]}
"""
import json
import os
import sys

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import pandas as pd  # noqa: E402
import lightgbm as lgb  # noqa: E402


def main() -> None:
    p = json.load(open(sys.argv[1]))
    X = pd.read_parquet(p["x"])
    if p["op"] == "fit":
        y = pd.read_parquet(p["y"])["y"]
        est = lgb.LGBMRegressor(n_estimators=p["n_estimators"],
                                random_state=p["random_state"], verbose=-1)
        est.fit(X.values, y.values)
        est.booster_.save_model(p["booster"])
    elif p["op"] == "predict":
        booster = lgb.Booster(model_file=p["booster"])
        pd.DataFrame({"yhat": booster.predict(X.values)}).to_parquet(p["out"])
    else:
        raise SystemExit(f"unknown op {p['op']}")
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()
