"""静止检测器(B',2026-08-26 事故的主探测器)的单元测试。

事故: RNN 层因 serve 漏写通用分支,从进生产第一天(8/14 灰度)到 8/25 发布的
pred_V 逐位不变,而唯二的旧探测器都瞎了 —— A/B 因候选臂与生产臂同源恒 False,
refreeze_due 是纯日历计数。这个检测器是补上的第一道确定性防线,它自己必须有测试。

全部用合成工件在 tmp 下跑,生产 outputs 零接触。
"""
import numpy as np
import pandas as pd
import pytest

from VolumePrediction import service


def _write(p, rows):
    """rows = [(ticker, pred_V, model_version), ...]"""
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=["ticker", "pred_V", "model_version"]
                 ).to_parquet(p, index=False)


@pytest.fixture()
def ops():
    return service.VolumeService().ops


def test_frozen_layer_is_flagged(tmp_path, ops):
    """一层逐位不变 → identical_frac == 1.0;另一层照常变动 → 0.0。"""
    n = 200
    tk = [f"T{i:04d}" for i in range(n)]
    frozen = list(np.linspace(1e6, 9e6, n))
    d1 = tmp_path / "a.parquet"
    d2 = tmp_path / "b.parquet"
    _write(d1, [(t, v, "rnn_x") for t, v in zip(tk, frozen)]
           + [(f"M{i}", 1e5 + i, "baselines.ma5") for i in range(n)])
    _write(d2, [(t, v, "rnn_x") for t, v in zip(tk, frozen)]     # 逐位相同
           + [(f"M{i}", 2e5 + i, "baselines.ma5") for i in range(n)])
    res = ops._pairwise_static(d2, d1)
    assert res["rnn_x"]["identical_frac"] == 1.0
    assert res["baselines.ma5"]["identical_frac"] == 0.0


def test_layer_churn_does_not_dilute(tmp_path, ops):
    """P1 回归(2026-08-26): 层间换票不得把冻死层的 identical_frac 稀释下去。

    初版 _static_check 把 prv 取成**整份工件**按 ticker 索引(所有层混一起),
    于是"今天在 A 层的票 vs 昨天它在 B 层的值"跨层比 —— 必然不同,漏报。
    实测代价: 8/17 RNN 层真值 1.0(已冻死),混层算法给 0.975 < 0.99 阈值不
    告警,拖到 8/19 全层换完才炸,白丢两天(8/18 混层值更低,0.500)。
    本例复刻那天的形态: 一半票昨天在 lgbm 层、今天被划进已冻死的 rnn 层。
    """
    n = 200
    old_rnn = [f"R{i:04d}" for i in range(n)]        # 昨天就在 rnn 层
    moved = [f"L{i:04d}" for i in range(n)]          # 昨天 lgbm,今天划进 rnn
    frozen = {t: 1e6 + i for i, t in enumerate(old_rnn)}
    d1 = tmp_path / "a.parquet"
    d2 = tmp_path / "b.parquet"
    _write(d1, [(t, frozen[t], "rnn_x") for t in old_rnn]
           + [(t, 5e6 + i, "lgbm_y") for i, t in enumerate(moved)])
    _write(d2, [(t, frozen[t], "rnn_x") for t in old_rnn]        # 冻死,一位没变
           + [(t, 7e6 + i, "rnn_x") for i, t in enumerate(moved)])  # 新划入,值不同
    res = ops._pairwise_static(d2, d1)
    assert res["rnn_x"]["identical_frac"] == 1.0, (
        "层内对拍必须只比两天都在该层的票;把新划入的票算进去会稀释成 0.5 "
        "而躲过 0.99 阈值 —— 这正是事故里丢掉的那两天")
    assert res["rnn_x"]["n"] == n, "样本必须收敛到同层同票的交集"


def test_new_layer_first_day_is_skipped(tmp_path, ops):
    """某层昨天不存在(首日上线)→ 不比,不得凭空产生 0 或 1。"""
    d1 = tmp_path / "a.parquet"
    d2 = tmp_path / "b.parquet"
    _write(d1, [(f"T{i}", 1e6 + i, "ma5") for i in range(100)])
    _write(d2, [(f"T{i}", 1e6 + i, "ma5") for i in range(100)]
           + [(f"N{i}", 2e6 + i, "brand_new") for i in range(100)])
    res = ops._pairwise_static(d2, d1)
    assert "brand_new" not in res
    assert res["ma5"]["identical_frac"] == 1.0


def test_min_n_guard(tmp_path, ops):
    """样本太小的层不出结论(避免几只票的巧合触发告警)。"""
    d1 = tmp_path / "a.parquet"
    d2 = tmp_path / "b.parquet"
    _write(d1, [(f"T{i}", 1e6, "tiny") for i in range(10)])
    _write(d2, [(f"T{i}", 1e6, "tiny") for i in range(10)])
    assert ops._pairwise_static(d2, d1, min_n=50) == {}
    assert ops._pairwise_static(d2, d1, min_n=5)["tiny"]["identical_frac"] == 1.0


def test_dormant_layer_is_checked(tmp_path, monkeypatch, ops):
    """休眠回退层(反事实存档)也要体检。

    RNN 吃满学习层后 lgbm 不再出现在发布工件里(8/18 起),可它仍是
    set_blend(False) 的回退目标 —— 回退到一个没人体检过的层 = 第二次事故。
    反事实存档每天存的正是被覆盖前的 lgbm 行,零额外算力。
    """
    hist = tmp_path / "history"
    for d in ("2026-09-01", "2026-09-02"):           # 发布层健康
        _write(hist / f"volume_forecast_{d}.parquet",
               [(f"T{i}", 1e6 + i + (0 if d.endswith("01") else 1), "rnn_x")
                for i in range(100)])
    for d in ("2026-09-01", "2026-09-02"):           # 休眠 lgbm 冻死
        _write(hist / f"counterfactual_noblend_{d}.parquet",
               [(f"T{i}", 3e6 + i, "lgbm_y") for i in range(100)])
    monkeypatch.setattr(type(ops.s), "art",
                        property(lambda self: tmp_path), raising=False)
    res = ops._static_check()
    assert res["rnn_x"]["identical_frac"] == 0.0
    assert res["dormant:lgbm_y"]["identical_frac"] == 1.0, \
        "休眠回退层冻死必须被发现,否则 set_blend(False) 会退到一个死层上"
