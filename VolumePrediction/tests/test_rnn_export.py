"""rnn_export 对拍: 纯 numpy 前向 ≡ torch 前向(E4 生产化红线)。

服务端不 import torch 的前提是导出权重后 numpy 复刻逐位可信 —— 本测试是
该前提的守门人。任何改动 _RNNCore 结构/门序/激活的提交都应在此处失败。
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[2]

torch = pytest.importorskip("torch")

from VolumePrediction.models.deep import PaperRNN, _sort_panel      # noqa: E402
from VolumePrediction.rnn_export import export_weights, RNNWeights  # noqa: E402


def _panel(n_feat=14, n_days=60, tickers=("AAA", "BBB", "CCC")):
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2024-01-02", periods=n_days)
    ix = pd.MultiIndex.from_product([dates, list(tickers)], names=["date", "ticker"])
    X = pd.DataFrame(rng.normal(size=(len(ix), n_feat)).astype("float32"),
                     index=ix, columns=[f"tech_f{i}" for i in range(n_feat)])
    y = pd.Series(rng.normal(size=len(ix)), index=ix)
    return X, y


def test_numpy_forward_matches_torch(tmp_path):
    X, y = _panel()
    m = PaperRNN(X.shape[1], seed=5, device="cpu").fit(X, y, epochs=2)
    p_torch = m.predict(X)

    w = RNNWeights.load(export_weights(m, tmp_path / "w.npz"))
    Xs, _, order = _sort_panel(X)
    p_sorted = w.predict_panel(Xs.values, Xs.index.get_level_values(1).values)
    inv = np.empty_like(order)
    inv[order] = np.arange(len(order))

    assert np.abs(p_torch.values - p_sorted[inv]).max() < 1e-4


def test_sigmoid_is_overflow_safe():
    """极端输入不得触发 RuntimeWarning(2026-08-03 实盘工件曾触发 exp 溢出)。"""
    import warnings

    from VolumePrediction.rnn_export import _sigmoid

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        s = _sigmoid(np.array([-800.0, -50.0, 0.0, 50.0, 800.0], dtype=np.float32))
    assert np.isfinite(s).all() and ((s >= 0) & (s <= 1)).all()
    # 非溢出区与朴素式逐位相同(修复未改变数值语义)
    mid = np.array([-20.0, -1.0, 0.0, 1.0, 20.0], dtype=np.float32)
    assert np.array_equal(_sigmoid(mid), (1.0 / (1.0 + np.exp(-mid))).astype(np.float32))


def test_export_meta_and_zero_pad_semantics(tmp_path):
    X, y = _panel(n_feat=8, n_days=15)
    m = PaperRNN(8, seed=1, device="cpu").fit(X, y, epochs=1)
    w = RNNWeights.load(export_weights(m, tmp_path / "w.npz"))
    assert (w.seq_len, w.n_pred, w.H) == (10, 8, 32)

    # 单票不足 seq_len 的头部行: 与显式零填充窗口逐位相同
    feats = np.arange(3 * 8, dtype=np.float32).reshape(3, 8)
    tk = np.array(["A", "A", "A"])
    got = w.predict_panel(feats, tk)
    man = np.zeros((3, 10, 8), dtype=np.float32)
    for i in range(3):
        man[i, 10 - (i + 1):, :] = feats[: i + 1]
    assert np.allclose(got, w.predict_windows(man), atol=1e-6)


def test_serving_path_runs_with_torch_blocked(tmp_path):
    """红线: 服务端 load+predict 必须在 torch 完全不可导入时也能跑通。

    源码里搜 'torch' 字样会被 docstring 误伤,故改用功能性验证 —— 子进程中
    用 meta_path 钩子让 `import torch` 直接抛错,再走一遍推理并对拍数值。
    """
    import json
    import subprocess
    import sys
    import textwrap

    X, y = _panel(n_feat=6, n_days=30, tickers=("AAA", "BBB"))
    m = PaperRNN(6, seed=2, device="cpu").fit(X, y, epochs=1)
    wpath = export_weights(m, tmp_path / "w.npz")
    Xs, _, _ = _sort_panel(X)
    np.save(tmp_path / "feats.npy", Xs.values)
    np.save(tmp_path / "tk.npy", Xs.index.get_level_values(1).values.astype(str))
    expect = RNNWeights.load(wpath).predict_panel(
        Xs.values, Xs.index.get_level_values(1).values)

    script = textwrap.dedent(f"""
        import sys, json
        import numpy as np

        class _Block:
            def find_module(self, name, path=None):
                return self if name == "torch" or name.startswith("torch.") else None
            def find_spec(self, name, path=None, target=None):
                if name == "torch" or name.startswith("torch."):
                    raise ImportError("torch blocked by red-line test")
                return None
            def load_module(self, name):
                raise ImportError("torch blocked by red-line test")

        sys.meta_path.insert(0, _Block())
        sys.path.insert(0, {str(_REPO)!r})
        from VolumePrediction.rnn_export import RNNWeights
        w = RNNWeights.load({str(wpath)!r})
        f = np.load({str(tmp_path / "feats.npy")!r})
        t = np.load({str(tmp_path / "tk.npy")!r}, allow_pickle=True)
        out = w.predict_panel(f, t)
        assert "torch" not in sys.modules, "服务路径导入了 torch"
        print(json.dumps(out.tolist()))
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-2000:]
    got = np.array(json.loads(r.stdout.strip().splitlines()[-1]), dtype=np.float32)
    assert np.allclose(got, expect, atol=1e-6)
