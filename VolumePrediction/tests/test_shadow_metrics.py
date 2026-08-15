"""shadow_rnn._metrics 的 econ 修正列(E10 议题③①,2026-08-15 接线)。

背景实证(8/13): regret 是相对量,loss_opt→0 时单票放大 10⁵ 倍
(XHG $V 一日跳 25 万倍 → regret 782,901%,占总量 84%)。修正:
econ_w=p99 winsorize;econ_abs=绝对损失差;λ 接自家 Amihud 标定。
"""
import numpy as np
import pandas as pd
import pytest

from VolumePrediction.shadow_rnn import _metrics


def _mk(n=500, seed=7):
    rng = np.random.default_rng(seed)
    a = pd.Series(np.exp(rng.normal(16, 2, n)))
    p = a * np.exp(rng.normal(0, .3, n))
    return p, a


def test_winsorized_econ_immune_to_single_outlier():
    p, a = _mk()
    p.iloc[0], a.iloc[0] = 1e2, 1e8        # XHG 型: 预测比实际低 6 个量级
    m = _metrics(p, a, mu=1e-4)
    clean = _metrics(p.drop(p.index[0]), a.drop(a.index[0]), mu=1e-4)
    assert m["econ"] > 1e3                 # 裸 regret 被单票打爆(病征保留可见)
    assert m["econ_w"] < clean["econ"] * 2 + 0.01   # winsorize 后回到干净量级
    assert m["econ_abs"] is not None and np.isfinite(m["econ_abs"])


def test_lambda_source_follows_config():
    p, a = _mk()
    m = _metrics(p, a, mu=1e-4)
    # config econ.lambda_form=calibrated → 标注自家标定;
    # 工件缺失时 policy 降级会标 paper_prior —— 两者都合法,绝不为 None
    assert m["lambda_source"] in ("amihud_market_proxy", "paper_prior")


def test_metrics_below_min_n_all_none():
    p, a = _mk(n=5)
    m = _metrics(p, a, mu=1e-4)
    assert m["econ"] is m["econ_w"] is m["econ_abs"] is None


def test_econ_none_without_mu():
    p, a = _mk()
    m = _metrics(p, a, mu=None)
    assert m["econ"] is None and m["econ_w"] is None
    assert m["mape"] is not None
