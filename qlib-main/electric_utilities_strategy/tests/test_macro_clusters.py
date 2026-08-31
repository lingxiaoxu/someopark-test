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
    from electric_utilities_strategy import macro_clusters as mc
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
    from electric_utilities_strategy import macro_clusters as mc
    from electric_utilities_strategy.smart_select import macro_positioning
    if not mc.AE_ARTIFACT.exists():
        pytest.skip("no production encoder artifact")
    r = macro_positioning(date(2026, 8, 14), pd.DataFrame())
    assert r["available"] is True, r["reason"]
    assert r["nearest_cluster"] is not None
    assert len(r["today_latent"]) == 12


# ═══ 生产入口全覆盖(2026-08-16 用户令: 每个入口都测,零生产写入)══════════

def _snapshot_dirs():
    """写入审计: 相关生产目录的 (path → mtime,size) 快照。"""
    import os
    roots = [_ROOT / "qlib-main" / "electric_utilities_strategy",
             _ROOT / "macro_similarity",
             _ROOT / "price_data" / "macro" / "state"]
    snap = {}
    for r in roots:
        for dirpath, dirnames, filenames in os.walk(r):
            if "__pycache__" in dirpath or "/mlruns" in dirpath or "/logs" in dirpath:
                # /logs 排除:集成 runner 把 pytest 输出 tee 进 logs/qa/,审计者
                # 会看到自己正在写的日志(自我观测假阳性,2026-08-31 实测)
                continue
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                    snap[p] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
    return snap


@pytest.fixture()
def prod_write_audit():
    before = _snapshot_dirs()
    yield
    after = _snapshot_dirs()
    new = set(after) - set(before)
    changed = {p for p in set(after) & set(before) if after[p] != before[p]}
    assert not new and not changed, \
        f"测试触碰了生产目录! 新增={sorted(new)[:5]} 修改={sorted(changed)[:5]}"


def _store_macro_df():
    """23 维 store 历史(读),当作 daily 传入的 macro_df(避免 load_macro
    自愈路径 —— 它可能对生产 store 追加当日快照)。"""
    from MacroStateStore import MacroStateStore
    return MacroStateStore().load()


def test_entry_smart_param_select_full(prod_write_audit):
    """生产入口①: AEUSdailySignal L958 的 smart_param_select 全三层。"""
    from datetime import date
    from electric_utilities_strategy.smart_select import (smart_param_select,
                                                     _load_selected_state)
    r = smart_param_select(date(2026, 8, 14), _store_macro_df(),
                           current_state=_load_selected_state())
    assert isinstance(r, dict) and "param_set" in r, r
    mp = r.get("macro_positioning") or {}
    assert mp.get("available") is True, f"Layer1 不可用: {mp.get('reason')}"


def test_entry_mcps_realtime_scores_no_delegation(prod_write_audit, caplog):
    """生产入口②: Layer 2 实时打分 —— 必须出分且不再触发 6 维委托告警。"""
    import logging
    from datetime import date
    from electric_utilities_strategy.smart_select import (mcps_realtime_scores,
                                                     _load_json, _CACHE_DIR)
    cands = _load_json(_CACHE_DIR / "top_candidates.json")
    cand_list = (cands if isinstance(cands, list)
                 else cands.get("top") or cands.get("candidates") or [])
    if not cand_list:
        pytest.skip("no top_candidates cache")
    with caplog.at_level(logging.WARNING):
        scores = mcps_realtime_scores(date(2026, 8, 14), _store_macro_df(),
                                      cand_list)
    assert scores, "MCPS 打分为空"
    assert not [r for r in caplog.records
                if "no compression possible" in r.getMessage()], \
        "仍在触发 6 维委托告警"


def test_entry_macro_weight_tilt(prod_write_audit):
    """生产入口③: AEUSdailySignal L1181 的 macro_weight_tilt。"""
    from datetime import date
    import pandas as pd
    from electric_utilities_strategy.smart_select import macro_weight_tilt
    w = pd.Series({"logic_cpu": 0.34, "memory_hbm": 0.33, "equipment": 0.33})
    out = macro_weight_tilt(w, _store_macro_df(), date(2026, 8, 14))
    assert abs(float(out.sum()) - 1.0) < 1e-6, f"权重未归一: {out.sum()}"
    assert ((out - w).abs() / w).max() < 0.12, "倾斜幅度越界(>±5% 复归一容差)"
