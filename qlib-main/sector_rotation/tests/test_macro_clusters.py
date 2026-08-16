"""SSRS macro_clusters 真修 B(2026-08-16)—— 与 semiconductor_strategy 同病同方;
引擎/build 机制的完整测试在 semiconductor_strategy/tests/test_macro_clusters.py,
此处只验 SSRS 自己的工件与 serving 端(生产只读,零写入)。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT), str(_ROOT / "qlib-main")):
    if p not in sys.path:
        sys.path.insert(0, p)


def test_ssrs_macro_positioning_e2e_readonly():
    from datetime import date
    import pandas as pd
    from sector_rotation import macro_clusters as mc
    from sector_rotation.smart_select import macro_positioning
    if not mc.AE_ARTIFACT.exists():
        pytest.skip("no production encoder artifact")
    r = macro_positioning(date(2026, 8, 14), pd.DataFrame())
    assert r["available"] is True, r["reason"]
    assert r["nearest_cluster"] is not None
    assert len(r["today_latent"]) == 12


def test_ssrs_artifacts_dimension_consistent():
    import numpy as np
    from sector_rotation import macro_clusters as mc
    from SimilarityEngine import AutoencoderMethod
    if not mc.AE_ARTIFACT.exists():
        pytest.skip("no production encoder artifact")
    cen = np.load(mc.CENTROIDS_PATH)
    enc = AutoencoderMethod.load(mc.AE_ARTIFACT)
    assert cen.shape[1] == enc.latent_dim == 12
    assert len(enc._feature_names) >= mc.MIN_AE_FEATURES


# ═══ 生产入口全覆盖(与 semiconductor 同款;零生产写入审计)═════════════════

import numpy as np


def _snapshot_dirs():
    import os
    roots = [_ROOT / "qlib-main" / "sector_rotation",
             _ROOT / "macro_similarity",
             _ROOT / "price_data" / "macro" / "state"]
    snap = {}
    for r in roots:
        for dirpath, dirnames, filenames in os.walk(r):
            if "__pycache__" in dirpath or "/mlruns" in dirpath:
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
    from MacroStateStore import MacroStateStore
    return MacroStateStore().load()


def test_entry_smart_param_select_full(prod_write_audit):
    """生产入口①: SectorRotationDailySignal L849 的 smart_param_select。"""
    from datetime import date
    from sector_rotation.smart_select import (smart_param_select,
                                              _load_selected_state)
    r = smart_param_select(date(2026, 8, 14), _store_macro_df(),
                           current_state=_load_selected_state())
    assert isinstance(r, dict) and "param_set" in r, r
    mp = r.get("macro_positioning") or {}
    assert mp.get("available") is True, f"Layer1 不可用: {mp.get('reason')}"


def test_entry_mcps_scores_no_delegation(prod_write_audit, caplog):
    import logging
    from datetime import date
    from sector_rotation.smart_select import (mcps_realtime_scores,
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
                if "no compression possible" in r.getMessage()]


def test_entry_macro_weight_tilt(prod_write_audit):
    """生产入口③: SectorRotationDailySignal L1052 的 macro_weight_tilt。"""
    from datetime import date
    import pandas as pd
    from sector_rotation.smart_select import macro_weight_tilt
    w = pd.Series({"XLK": 0.34, "XLV": 0.33, "XLE": 0.33})
    out = macro_weight_tilt(w, _store_macro_df(), date(2026, 8, 14))
    assert abs(float(out.sum()) - 1.0) < 1e-6, f"权重未归一: {out.sum()}"
