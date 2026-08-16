"""macro_clusters 真修 B(2026-08-16)+ SimilarityEngine 新 API 测试。

沙箱纪律: 生产 store 只读;一切落盘进 tmp_path。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[3]              # someopark-test/
for p in (str(_ROOT), str(_ROOT / "qlib-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

from SimilarityEngine import AutoencoderMethod  # noqa: E402


def _synth(n=300, k=23, seed=7):
    rng = np.random.default_rng(seed)
    names = [f"f{i}" for i in range(k)]
    mat = rng.normal(0, 1, (n, k)).astype(np.float32).cumsum(axis=0) * 0.01 + 5
    return mat, names


def test_is_pca_initialized():
    """weekly 降级根因之一: 未训练实例缺 _is_pca → AttributeError。"""
    m = AutoencoderMethod()
    assert m._is_pca is False and m._trained is False


def test_latent_of_consistent_with_compute_weights():
    """latent_of 与 compute_weights 必须同一条变换链: 用 latent_of 的输出
    重算高斯核权重,应与 compute_weights 逐位一致。"""
    mat, names = _synth()
    today = mat[-1].copy()
    m1 = AutoencoderMethod(epochs=10)
    w = m1.compute_weights(mat, today.copy(), names)
    m2 = AutoencoderMethod(epochs=10)
    lm, lt = m2.latent_of(mat, today.copy(), names)
    d = np.sqrt(((lm - lt) ** 2).sum(axis=1))
    sigma = max(float(np.median(d)), 1e-3)
    w2 = np.exp(-(d ** 2) / (2 * sigma ** 2))
    assert np.allclose(w, w2, atol=1e-6), "两条 API 变换链分叉了"


def test_save_load_roundtrip_same_latent(tmp_path):
    """持久化 encoder 加载后编码逐位一致(serving 与重建同基底的前提)。"""
    mat, names = _synth()
    today = mat[-1].copy()
    m = AutoencoderMethod(epochs=10)
    _, lt_orig = m.latent_of(mat, today.copy(), names)
    p = tmp_path / "enc.pt"
    m.save(p)
    m2 = AutoencoderMethod.load(p)
    assert m2._trained and not m2._is_pca
    _, lt_loaded = m2.latent_of(mat, today.copy(), names)
    assert np.allclose(lt_orig, lt_loaded, atol=1e-6)


def test_feature_mismatch_raises(tmp_path):
    """load 后特征序不一致必须 raise —— 静默错位编码是最坏结局。"""
    mat, names = _synth()
    m = AutoencoderMethod(epochs=5)
    m.latent_of(mat, mat[-1].copy(), names)
    p = tmp_path / "enc.pt"
    m.save(p)
    m2 = AutoencoderMethod.load(p)
    wrong = list(reversed(names))
    with pytest.raises(ValueError, match="feature mismatch"):
        m2.latent_of(mat, mat[-1].copy(), wrong)


def test_dimension_guard_returns_none_not_crash():
    """≤ latent_dim 维: latent_of 干净 (None, None),绝不 AttributeError。"""
    mat, names = _synth(k=6)
    m = AutoencoderMethod()
    lm, lt = m.latent_of(mat, mat[-1].copy(), names)
    assert lm is None and lt is None


def test_save_refuses_untrained():
    m = AutoencoderMethod()
    with pytest.raises(RuntimeError):
        m.save("/tmp/should_not_exist.pt")


def test_build_end_to_end_sandbox(tmp_path):
    """macro_clusters.build 全链(真 store 只读,落盘全进 tmp):
    encoder/centroids/cluster json 三件套齐 + 维度一致 + 备份不裸覆盖。"""
    from semiconductor_strategy import macro_clusters as mc
    hist, why = mc.load_full_macro_history()
    if hist is None:
        pytest.skip(f"macro store unavailable: {why}")
    folds = [{"oos_start": "2024-01-05", "oos_end": "2024-06-28",
              "all_oos_sharpes": {"default": 0.5, "alt": 0.2}},
             {"oos_start": "2024-07-01", "oos_end": "2024-12-31",
              "all_oos_sharpes": {"default": -0.1, "alt": 0.9}},
             {"oos_start": "2025-01-02", "oos_end": "2025-06-30",
              "all_oos_sharpes": {"default": 1.1, "alt": 0.3}},
             {"oos_start": "2025-07-01", "oos_end": "2025-12-31",
              "all_oos_sharpes": {"default": 0.2, "alt": 0.6}}]
    s = mc.build(folds, out_dir=tmp_path)
    assert s["n_folds_used"] == 4 and s["n_features"] >= mc.MIN_AE_FEATURES
    cen = np.load(tmp_path / "macro_latent_centroids.npy")
    enc = AutoencoderMethod.load(tmp_path / "macro_ae_encoder.pt")
    assert cen.shape[1] == enc.latent_dim
    oos = json.loads((tmp_path / "param_oos_by_macro_cluster.json").read_text())
    assert set(oos) == {"default", "alt"}
    # 重建第二次: 旧件必须有 .bak_ 备份
    mc.build(folds, out_dir=tmp_path)
    assert list(tmp_path.glob("macro_latent_centroids.npy.bak_*"))


def test_macro_positioning_e2e_readonly():
    """serving E2E(生产工件只读): available=True 且 latent 维与 centroids 齐。"""
    from datetime import date
    import pandas as pd
    from semiconductor_strategy import macro_clusters as mc
    from semiconductor_strategy.smart_select import macro_positioning
    if not mc.AE_ARTIFACT.exists():
        pytest.skip("no production encoder artifact")
    r = macro_positioning(date(2026, 8, 14), pd.DataFrame())
    assert r["available"] is True, r["reason"]
    assert r["nearest_cluster"] is not None
    assert len(r["today_latent"]) == 12
