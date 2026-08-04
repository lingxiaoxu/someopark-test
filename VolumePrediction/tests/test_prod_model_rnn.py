"""prod_model_rnn freeze/serve 端到端(小合成面板;全部落 tmp,零生产写入)。

覆盖: 工件完整性 / 窗拼接正确性(seq_tail+当日行 ≡ 手工窗) / seed 平均 /
active 过滤 / 幂等滚动 / 断档拒绝出数。
"""
import json

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from VolumePrediction import prod_model_rnn as pmr                       # noqa: E402


def _panel(n_days=40, tickers=("AAA", "BBB", "CCC")):
    rng = np.random.default_rng(4)
    dates = pd.bdate_range("2026-01-02", periods=n_days)
    ix = pd.MultiIndex.from_product([dates, list(tickers)], names=["date", "ticker"])
    n = len(ix)
    df = pd.DataFrame(index=ix)
    for s in ("ret", "v"):
        for w in (1, 5, 22, 252):
            df[f"tech_{s}_ma{w}"] = rng.normal(size=n).astype("float32")
    for j in range(6):
        df[f"fund1_f{j}"] = rng.normal(size=n).astype("float32")
    df["ret"] = rng.normal(scale=0.01, size=n)
    df["v"] = 10 + rng.normal(scale=0.2, size=n)
    df["ma5_v"] = 10.0
    df["eta"] = df["v"] - df["ma5_v"]
    return df, dates


@pytest.fixture(scope="module")
def frozen(tmp_path_factory, monkeypatch_module=None):
    df, dates = _panel()
    d = tmp_path_factory.mktemp("rnn_art")
    ppath = d / "panel_test_rnnp.parquet"
    df.to_parquet(ppath)
    asof = str(dates[-1].date())
    nxt = str((dates[-1] + pd.offsets.BDay(1)).date())

    import VolumePrediction.data.polygon_loader as pl
    orig = pl.trading_days
    pl.trading_days = lambda a, b: [asof, nxt]
    try:
        meta = pmr.freeze(ppath, asof, art_dir=d / "art", seeds=2, epochs=1)
    finally:
        pl.trading_days = orig
    return d / "art", meta, df, asof, nxt


def test_freeze_artifacts(frozen):
    art, meta, df, asof, nxt = frozen
    assert meta["kind"] == "learned.rnn" and meta["seq_len"] == 10
    assert len(meta["feature_cols"]) == 14          # tech 8 + fund1 6
    assert all(not c.startswith(("fund2_", "cal_", "earn_")) for c in meta["feature_cols"])
    for f in ["per_ticker.parquet", "seq_tail.npz", "meta.json"] + meta["weight_files"]:
        assert (art / f).exists(), f
    z = np.load(art / "seq_tail.npz", allow_pickle=False)
    assert z["feats"].shape == (3, 9, 14)
    # seq_tail 就是面板每票末 9 行
    last9 = df[meta["feature_cols"]].groupby(level="ticker").tail(9)
    for i, tk in enumerate(z["tickers"].astype(str)):
        exp = last9.xs(tk, level="ticker").values.astype("float32")
        assert np.allclose(z["feats"][i], exp, atol=1e-6)


def test_serve_window_and_seed_average(frozen, monkeypatch):
    art, meta, df, asof, nxt = frozen
    import VolumePrediction.data.polygon_loader as pl
    monkeypatch.setattr(pl, "trading_days", lambda a, b: [asof, nxt])

    out = pmr.serve(art, nxt)
    assert set(out.columns) >= {"date", "ticker", "pred_v", "pred_eta", "model_version"}
    assert out["date"].iloc[0] == asof and len(out) == 3

    # 手工复算: 窗 = seq_tail(9) + 当日行(z_next tech + fund1 末值), 两 seed 均值
    from VolumePrediction.rnn_export import RNNWeights
    per = pd.read_parquet(art / "per_ticker.parquet")
    z = np.load(art / "seq_tail.npz", allow_pickle=False)
    cols = meta["feature_cols"]
    tech = per[[f"{c}__z_next" for c in pmr.TECH_COLS]].copy()
    tech.columns = pmr.TECH_COLS
    X = tech.join(per[meta["fund_cols"]], how="left")[cols].fillna(0.0)
    tk = list(z["tickers"].astype(str))
    W = np.concatenate([z["feats"],
                        X.loc[tk].values.astype("float32")[:, None, :]], axis=1)
    man = np.mean([RNNWeights.load(art / f).predict_windows(W)
                   for f in meta["weight_files"]], axis=0)
    got = out.set_index("ticker").loc[tk, "pred_eta"].values
    assert np.allclose(got, man, atol=1e-5)


def test_gap_refuses_to_serve(frozen, monkeypatch):
    """seq_tail 断档时必须抛错,不得静默用错位窗出数。"""
    art, meta, df, asof, nxt = frozen
    far = str((pd.Timestamp(nxt) + pd.offsets.BDay(5)).date())
    import VolumePrediction.data.polygon_loader as pl
    monkeypatch.setattr(pl, "trading_days",
                        lambda a, b: [asof, nxt, far])
    with pytest.raises(RuntimeError, match="断档"):
        pmr.serve(art, far)


def test_roll_is_idempotent(frozen, monkeypatch, tmp_path):
    import shutil

    art, meta, df, asof, nxt = frozen
    art2 = tmp_path / "art_copy"
    shutil.copytree(art, art2)
    import VolumePrediction.data.polygon_loader as pl
    monkeypatch.setattr(pl, "trading_days", lambda a, b: [asof, nxt])

    pmr.serve(art2, nxt, update_state=True)
    z1 = np.load(art2 / "seq_tail.npz", allow_pickle=False)["feats"].copy()
    m1 = json.loads((art2 / "meta.json").read_text())["seq_tail_date"]
    pmr.serve(art2, nxt, update_state=True)          # 同日重复
    z2 = np.load(art2 / "seq_tail.npz", allow_pickle=False)["feats"]
    # 戳 = 窗口末行日期 = 刚服务的 target(不是 prev);且同日不二次滚动
    assert m1 == nxt, f"seq_tail_date 应为 {nxt},实为 {m1}"
    assert np.array_equal(z1, z2), "同日重复 serve 二次滚动了窗"
    # 窗口确实前进了一格: 末行 = 当日特征行
    z0 = np.load(art / "seq_tail.npz", allow_pickle=False)["feats"]
    assert not np.array_equal(z0, z1), "窗口未前进"
