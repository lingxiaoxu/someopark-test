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
