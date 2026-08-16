"""macro_clusters — AISS Layer-1 宏观定位的离线重建(2026-08-16 真修 B)。

历史病灶(三层,weekly success-degraded 根因):
  1) 喂数残缺: AISS 管道传入的 macro_df 只有 10 列,与 23 维
     AUTOENCODER_FEATURES 交集仅 6 → 7/31 引擎守卫正确拒训 → AttributeError;
  2) 私掏 API: smart_select/AISSBatchRun 直塞**生向量**进 _encode,跳过
     prepass/log/标准化 —— 即便在旧代码下,latent 也是尺度噪音;
  3) 基底漂移: serving 每日惰性重训 autoencoder,latent 基底随数据增长漂移,
     与离线 centroids 比距离无意义。

真修(本模块 = 单一事实源,standalone CLI 与 AISSBatchRun 共用):
  - 训练矩阵直接取根 MacroStateStore 的 **23 维全历史**(零缺失,2017→今);
  - 训练一次 → **持久化 encoder**(macro_ae_encoder.pt),serving 加载同一
    基底,绝不重训;
  - 每 fold OOS 宏观向量按原口径(OOS 期 mean)从 store 现算(旧 6 键
    oos_macro_vec 弃用),走 latent_of 完整变换链;
  - KMeans(≤6, seed=42)→ centroids + param_oos_by_macro_cluster 同批重建
    (cluster id 与 centroids 必须同一次 KMeans,错位=错先验)。

用法(qlib_run 环境):
    python -m semiconductor_strategy.macro_clusters            # 读 wf_fold_detail.json 重建
    python -m semiconductor_strategy.macro_clusters --dry-run  # 只训练+聚类,不落盘
"""
from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _THIS_DIR.parent.parent          # someopark-test 根
_CACHE_DIR = _THIS_DIR / "backtest_results"

AE_ARTIFACT = _CACHE_DIR / "macro_ae_encoder.pt"
CENTROIDS_PATH = _CACHE_DIR / "macro_latent_centroids.npy"
CLUSTER_OOS_PATH = _CACHE_DIR / "param_oos_by_macro_cluster.json"
FOLD_DETAIL_PATH = _CACHE_DIR / "wf_fold_detail.json"

MIN_AE_FEATURES = 16      # 23 维设计,容忍少量缺列;≤12(latent_dim)引擎会拒
MIN_HISTORY_ROWS = 250    # 训练底线(约一年)

log = logging.getLogger("semiconductor_strategy.macro_clusters")


def _ensure_root_path() -> None:
    if str(_PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(_PROJECT_DIR))


def load_full_macro_history():
    """→ (hist_df[feats], feats) 根 store 的 23 维全历史(dropna 后)。
    不足 MIN_AE_FEATURES/MIN_HISTORY_ROWS → (None, 原因) 大声不出数。"""
    _ensure_root_path()
    from MacroStateStore import MacroStateStore
    from SimilarityEngine import AUTOENCODER_FEATURES
    df = MacroStateStore().load()
    feats = [f for f in AUTOENCODER_FEATURES if f in df.columns]
    if len(feats) < MIN_AE_FEATURES:
        return None, f"store 仅 {len(feats)}/{len(AUTOENCODER_FEATURES)} 维 (<{MIN_AE_FEATURES})"
    sub = df[feats].dropna(how="any")
    if len(sub) < MIN_HISTORY_ROWS:
        return None, f"历史仅 {len(sub)} 行 (<{MIN_HISTORY_ROWS})"
    return sub, feats


def build(folds: List[dict], out_dir: Optional[Path] = None,
          dry_run: bool = False) -> dict:
    """folds: [{oos_start, oos_end, all_oos_sharpes}, ...](WF 留痕即够,
    不需要重跑 WF)。→ 摘要 dict;失败 raise(调用方决定 fail-open 与否)。"""
    _ensure_root_path()
    from MacroStateStore import MacroStateStore
    from SimilarityEngine import AutoencoderMethod
    from sklearn.cluster import KMeans

    out_dir = Path(out_dir) if out_dir else _CACHE_DIR
    hist, feats = load_full_macro_history()
    if hist is None:
        raise RuntimeError(f"macro history unavailable: {feats}")
    hist_mat = hist.values.astype(np.float32)

    # 训练(latent_of 首调惰性训练;today 用末行只为走完整链,不影响训练)
    method = AutoencoderMethod()
    latent_hist, _ = method.latent_of(hist_mat, hist_mat[-1].copy(), feats)
    if latent_hist is None:
        raise RuntimeError("autoencoder 未训练(维度守卫触发?)")

    # 每 fold OOS 宏观向量(原口径: OOS 期 mean),同一条变换链编码。
    # prepass(rolling-z)按 PIT 用 ≤ oos_end 的历史。
    store = MacroStateStore()
    fold_latents, fold_idx, skipped = [], [], []
    for i, f in enumerate(folds):
        vec = store.period_vector(start=f["oos_start"], end=f["oos_end"],
                                  features=feats, method="mean")
        if any(vec.get(k) is None for k in feats):
            skipped.append((i, "macro vec 有缺失"))
            continue
        arr = np.array([float(vec[k]) for k in feats], dtype=np.float32)
        hist_pit = hist[hist.index <= f["oos_end"]]
        if len(hist_pit) < MIN_HISTORY_ROWS:
            skipped.append((i, f"PIT 历史仅 {len(hist_pit)} 行"))
            continue
        _, lat = method.latent_of(hist_pit.values.astype(np.float32), arr, feats)
        fold_latents.append(lat)
        fold_idx.append(i)
    if len(fold_latents) < 4:
        raise RuntimeError(f"可编码 fold 仅 {len(fold_latents)} (<4);skipped={skipped}")

    X = np.array(fold_latents)
    n_clusters = min(6, len(X))
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    km.fit(X)

    # param_oos_by_macro_cluster(cluster id 与本次 KMeans 同批,绝不混用旧 id)
    from collections import defaultdict
    buckets: dict = defaultdict(lambda: defaultdict(list))
    for pos, i in enumerate(fold_idx):
        cl = int(km.labels_[pos])
        for ps, sr in (folds[i].get("all_oos_sharpes") or {}).items():
            if sr is not None and np.isfinite(sr):
                buckets[ps][f"cluster_{cl}"].append(float(sr))
    cluster_oos = {ps: {cl: {"mean_oos_sharpe": round(float(np.mean(v)), 4),
                             "n_folds": len(v)}
                        for cl, v in cls.items()}
                   for ps, cls in buckets.items()}

    summary = {"n_folds_used": len(fold_idx), "n_skipped": len(skipped),
               "n_clusters": n_clusters, "n_features": len(feats),
               "history_rows": len(hist),
               "cluster_sizes": np.bincount(km.labels_).tolist(),
               "built_at": datetime.now().isoformat(timespec="seconds")}
    if dry_run:
        log.info(f"[MACRO CLUSTERS] dry-run OK: {summary}")
        return summary

    # 落盘(旧工件时间戳备份,绝不裸覆盖)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for p in (AE_ARTIFACT, CENTROIDS_PATH, CLUSTER_OOS_PATH):
        tgt = out_dir / p.name
        if tgt.exists():
            shutil.copy2(tgt, tgt.with_suffix(tgt.suffix + f".bak_{stamp}"))
    out_dir.mkdir(parents=True, exist_ok=True)
    method.save(out_dir / AE_ARTIFACT.name)
    np.save(str(out_dir / CENTROIDS_PATH.name), km.cluster_centers_)
    (out_dir / CLUSTER_OOS_PATH.name).write_text(
        json.dumps(cluster_oos, indent=2))
    (out_dir / "macro_clusters_build_meta.json").write_text(
        json.dumps({**summary, "skipped": skipped}, indent=2))
    log.info(f"[MACRO CLUSTERS] rebuilt → {out_dir} {summary}")
    return summary


def main() -> int:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="AISS macro cluster rebuild (真修 B)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fold-detail", default=str(FOLD_DETAIL_PATH))
    a = ap.parse_args()
    detail = json.loads(Path(a.fold_detail).read_text())
    folds = [{"oos_start": f["oos_start"], "oos_end": f["oos_end"],
              "all_oos_sharpes": f.get("all_oos_sharpes") or {}}
             for f in detail["folds"]]
    s = build(folds, dry_run=a.dry_run)
    print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
