"""blend3 正式化(E10 实施-1,2026-08-15 代码就位;默认关,8/17 拍板后启用)。

只测接线决策与开关语义,不重跑重服务(与 test_refresh_dispatch_rnn 同纪律)。
"""
import inspect
import json

import pytest

from VolumePrediction import service


def test_refresh_blend_block_is_wired():
    """守卫: refresh 里确实存在 blend3 分层块(防回改)。"""
    src = inspect.getsource(service)
    assert '_blend_cfg.get("enabled")' in src
    assert "blend_routing" in src                     # 路由=单一事实源
    assert "blend_serve_" in src                      # 同日重跑缓存
    # RNN serve 必须用影子同款约定 serve(asof)(lgbm 的 _target=asof+1 会撞
    # seq_tail 断档守卫)
    assert "_pmr2.serve(_rart, asof" in src


def test_shadow_rnn_goes_readonly_when_blend_enabled():
    """seq_tail 单滚纪律: blend 启用后影子必须转只读。"""
    from VolumePrediction import shadow_rnn
    src = inspect.getsource(shadow_rnn.run_daily)
    assert "roll = False" in src
    assert "blend" in src


def test_set_blend_switch(tmp_path, monkeypatch):
    """set_blend 开关: 写 registry.blend、工件存在性校验、单行回退。"""
    svc = service.VolumeService()
    ops = svc.ops
    reg_state = {}
    monkeypatch.setattr(svc.registry, "load", lambda: dict(reg_state))
    monkeypatch.setattr(svc.registry, "save",
                        lambda d: reg_state.update(d))
    # 工件不存在 → 拒绝启用
    with pytest.raises(FileNotFoundError):
        ops.set_blend(True, rnn_version="rnn_missing_v0")
    # 无版本 → 拒绝启用
    with pytest.raises(ValueError):
        ops.set_blend(True)
    # 用真实存在的候选工件启停一轮(只动 monkeypatch 的内存 registry)
    real = "rnn_v6f32n_20260731"
    if not (svc.art / "registry" / "artifacts" / real).exists():
        pytest.skip("candidate artifact absent")
    b = ops.set_blend(True, rnn_version=real, by="test")
    assert b["enabled"] is True and b["variant"] == "blend3"
    assert reg_state["blend"]["rnn_version"] == real
    b2 = ops.set_blend(False, by="test")              # 单行回退
    assert b2["enabled"] is False
    assert reg_state["blend"]["rnn_version"] == real  # 版本保留,便于再启用


def test_registry_blend_default_disabled():
    """生产 registry.json 的 blend 键: 存在且 enabled=False(8/17 前不许开)。"""
    from VolumePrediction.common import OUT
    data = json.loads((OUT / "registry" / "registry.json").read_text())
    blend = data.get("blend")
    assert blend is not None, "blend 配置未 seed"
    assert blend["enabled"] is False
    assert blend["rnn_version"] == "rnn_v6f32n_20260731"


def test_refresh_blend_e2e_sandbox(tmp_path, monkeypatch):
    """端到端演练(全沙箱): blend 启用 → refresh 产出三层工件。
    真 raw 只读;工件目录=tmp;RNN serve 打桩(不动真候选 seq_tail)。"""
    import numpy as np
    import pandas as pd
    from VolumePrediction import prod_model_rnn
    from VolumePrediction.common import OUT

    real_raw = service.DATA_ROOT / "raw"
    if not real_raw.exists() or not list(real_raw.glob("grouped_*.parquet")):
        pytest.skip("no raw store")

    art = tmp_path / "outputs"
    (art / "registry" / "artifacts" / "rnn_fake").mkdir(parents=True)
    (art / "registry" / "artifacts" / "rnn_fake" / "meta.json").write_text(
        json.dumps({"kind": "learned.rnn", "seq_tail_date": "1970-01-01"}))
    (art / "registry" / "registry.json").write_text(json.dumps({
        "models": {}, "production": None,
        "blend": {"enabled": True, "variant": "blend3",
                  "rnn_version": "rnn_fake"}}))

    served = {}

    def fake_serve(a, target, update_state=False):
        served["target"], served["update_state"] = target, update_state
        raw_days = sorted(real_raw.glob("grouped_*.parquet"))
        day = pd.read_parquet(raw_days[-1])
        tk = day["ticker"].head(2000)
        return pd.DataFrame({
            "ticker": tk, "pred_v": 15.0, "pred_V": float(np.exp(15.0)),
            "pred_eta": 0.05, "model_version": "rnn_fake",
            "trained_through": "2026-07-31"})

    monkeypatch.setattr(prod_model_rnn, "serve", fake_serve)
    svc = service.VolumeService(artifacts_dir=art, raw_dir=real_raw)
    r = svc.ops.refresh(fetch=False)
    assert r["status"] == "ok"
    asof = r["asof"]
    assert served["update_state"] is True, "正式路径必须滚动 seq_tail"
    assert served["target"] == asof, "RNN serve 必须用影子同款 serve(asof) 约定"

    out = pd.read_parquet(art / "history" / f"volume_forecast_{asof}.parquet")
    mix = out["model_version"].value_counts().to_dict()
    assert mix.get("rnn_fake", 0) > 100, f"RNN 层为空: {mix}"
    assert mix.get("baselines.ma5", 0) > 100, f"ma5 兜底层为空: {mix}"
    # 当日缓存已写(同日重跑不降级)
    assert (art / "registry" / "artifacts" / "rnn_fake"
            / f"blend_serve_{asof}.parquet").exists()
