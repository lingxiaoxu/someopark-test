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


def test_serve_output_moves_day_to_day(tmp_path):
    """P0 回归(2026-08-26 事故): 连续两日 serve 的输出不得逐位相同。

    事故: serve() 只有 fast 分支,X 全部来自冻结的 per_ticker(z_next /
    ma5v_next 是为 first_serve_date 单日预算的常量),完全不含 target_date
    依赖。_roll_seq_tail 每天把同一行滚进窗,滚满 L=9 次后(2026-08-14)窗
    变成同一行的 10 份拷贝,η̂ 落到不动点 —— 8/14→8/26 连续 9 个交易日
    3,869 票预测值一个 bit 都没变,同期全市场成交额跌 17.8%,持仓票 MAPE
    25.7→56.4,而 refreeze 日历计数器和 A/B 都没报警。

    构造(关键): 两次 serve 都 **update_state=False**,窗字节完全相同 —— 于是
    输出的任何差异只可能来自"当日特征行随 target_date 重算"这条通路。反过来,
    只要 P0 被回改,X 与锚就重新与 target_date 无关,两日输出必然逐位相同,
    本断言立刻炸。(若改成 update_state=True 逐日滚,窗每天进一格,带 bug 的
    代码在前 9 天也会给出微小差异,要等窗饱和才暴露 —— 那样的测试抓不住。)

    只读: 用真工件的 tmp 副本,serve 不写状态,生产工件零接触。
    代价: 两次通用路径各读 ~330 个 raw 日,约 1 分钟。
    """
    import shutil

    from VolumePrediction.common import OUT
    from VolumePrediction.data import polygon_loader as pl

    src = OUT / "registry" / "artifacts" / "rnn_v6f32n_20260731"
    if not (src / "per_ticker.parquet").exists():
        pytest.skip("生产 RNN 工件不存在")
    raws = sorted(pl.RAW_DIR.glob("grouped_*.parquet"))
    if len(raws) < 300:
        pytest.skip("raw 存量不足以走通用路径")

    art = tmp_path / "rnn_art_copy"
    shutil.copytree(src, art)
    meta = json.loads((art / "meta.json").read_text())
    d2 = raws[-1].stem.split("_")[-1]
    d1 = pmr._prev_trading_day(d2)
    assert d1 > meta["first_serve_date"], "两个目标日都必须走通用路径"

    def _serve_at(target):
        m = json.loads((art / "meta.json").read_text())
        m["seq_tail_date"] = pmr._prev_trading_day(target)   # 只挪戳,不动窗
        (art / "meta.json").write_text(json.dumps(m))
        return pmr.serve(art, target).set_index("ticker")

    o1, o2 = _serve_at(d1), _serve_at(d2)
    common = o1.index.intersection(o2.index)
    assert len(common) > 1000, f"两日票交集过小: {len(common)}"
    same = np.isclose(o1.loc[common, "pred_V"].to_numpy(float),
                      o2.loc[common, "pred_V"].to_numpy(float),
                      rtol=1e-9, atol=0.0).mean()
    assert same < 0.5, (
        f"serve({d1}) 与 serve({d2}) 有 {same:.1%} 的票预测值逐位相同 —— "
        f"输出不随目标日变化,serve 已冻死(P0 被回改?)")
    # 锚(ma5v_next)本身也必须动: 冻结锚是这次事故里 pred_v 水平的直接病灶
    lvl = np.isclose(o1.loc[common, "pred_v"].to_numpy(float)
                     - o1.loc[common, "pred_eta"].to_numpy(float),
                     o2.loc[common, "pred_v"].to_numpy(float)
                     - o2.loc[common, "pred_eta"].to_numpy(float),
                     rtol=1e-9, atol=0.0).mean()
    assert lvl < 0.5, f"水平锚 ma5_v 有 {lvl:.1%} 逐位相同 —— 锚仍钉在冻结日"


def test_serve_general_path_is_wired():
    """结构守卫(无 raw 存量时 P0 的兜底): serve 必须有通用路径分支。

    上面的功能测试要真 raw 才跑得动;这条零依赖,防止有人回改后测试静默 skip。
    """
    import inspect

    src = inspect.getsource(pmr.serve)
    assert "_tech_and_ma5_from_raw" in src, "serve 缺通用路径(退回冻结常量)"
    # 水平锚必须走 ma5_src(fast=冻结值 / 否则=当日重算值),而不是在两条分支
    # 汇合之后无条件读 per_ticker["ma5v_next"] —— 后者是 7/31 的常量。
    assert "ma5 = ma5_src.reindex(keep)" in src
    assert 'ma5 = per_ticker["ma5v_next"]' not in src


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
