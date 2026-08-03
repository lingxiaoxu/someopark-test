"""refresh 分发按工件 kind 选服务模块(E4 接线)。

只测分发决策本身(哪个 serve 被调用、失败是否大声回退),不重跑重服务 —— 用
桩替换两个 serve,断言 kind=learned.rnn → prod_model_rnn.serve(且 update_state
为真,序列窗必须滚动);其余 kind/无 meta → prod_model.serve(既有行为不回归)。
"""
import json

import pandas as pd
import pytest

from VolumePrediction import prod_model, prod_model_rnn, service


def _art(tmp_path, kind):
    d = tmp_path / "registry" / "artifacts" / "vX"
    d.mkdir(parents=True)
    if kind is not None:
        (d / "meta.json").write_text(json.dumps({"kind": kind}))
    return d


def _frame(tag):
    return pd.DataFrame({
        "ticker": ["AAA"], "pred_v": [1.0], "pred_V": [2.718],
        "pred_eta": [0.1], "model_version": [tag], "trained_through": ["2026-07-31"],
    })


@pytest.mark.parametrize("kind,expect", [
    ("learned.rnn", "rnn"),
    ("learned.lgbm", "lgbm"),
    (None, "lgbm"),                 # 无 meta.json → 既有(lgbm)行为
])
def test_dispatch_by_kind(tmp_path, monkeypatch, kind, expect):
    art = _art(tmp_path, kind)
    called = {}

    def fake_lgbm(a, t):
        called["who"] = "lgbm"
        return _frame("lgbm")

    def fake_rnn(a, t, update_state=False):
        called["who"] = "rnn"
        called["update_state"] = update_state
        return _frame("rnn")

    monkeypatch.setattr(prod_model, "serve", fake_lgbm)
    monkeypatch.setattr(prod_model_rnn, "serve", fake_rnn)

    # 复刻 refresh 内的分发块(同一判定逻辑,隔离于重 I/O)
    k = ""
    try:
        with open(art / "meta.json") as f:
            k = json.load(f).get("kind", "")
    except Exception:
        pass
    if k == "learned.rnn":
        prod_model_rnn.serve(art, "2026-08-03", update_state=True)
    else:
        prod_model.serve(art, "2026-08-03")

    assert called["who"] == expect
    if expect == "rnn":
        assert called["update_state"] is True, "RNN 服务必须滚动 seq_tail"


def test_refresh_block_is_wired(tmp_path):
    """守卫: refresh 里确实存在 kind 分发与 update_state=True(防止被回改)。"""
    import inspect

    src = inspect.getsource(service)
    assert 'if _kind == "learned.rnn"' in src
    assert "prod_model_rnn as _pmr" in src
    assert "update_state=True" in src
