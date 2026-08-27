"""shadow_rnn._metrics 的 econ 修正列(E10 议题③①,2026-08-15 接线)。

背景实证(8/13): regret 是相对量,loss_opt→0 时单票放大 10⁵ 倍
(XHG $V 一日跳 25 万倍 → regret 782,901%,占总量 84%)。修正:
econ_w=p99 winsorize;econ_abs=绝对损失差;λ 接自家 Amihud 标定。
"""
import numpy as np
import pandas as pd
import pytest

from VolumePrediction.shadow_rnn import _metrics, _paired


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


# ── 逐票配对检验(用户批准 2026-08-26,10 日观察期的主证据)──────────────────
# 现有裁决是"当日聚合 MAPE 谁低"再数天数,10 天的 7/10 符号检验 p≈0.17 不显著。
# 持仓票每天 ~200 只,逐票配对后单日 n≈200、10 日累计 n≈2000,才有统计力。

def test_paired_detects_clear_winner():
    """RNN 系统性更准 → winrate 远超 0.5 且 p 极小。"""
    rng = np.random.default_rng(11)
    n = 300
    idx = [f"T{i}" for i in range(n)]
    a = pd.Series(np.exp(rng.normal(16, 2, n)), index=idx)
    good = a * np.exp(rng.normal(0, .10, n))        # 小误差
    bad = a * np.exp(rng.normal(0, .40, n))         # 大误差
    r = _paired(good, bad, a)
    assert r["n"] == n
    assert r["winrate"] > 0.7, r
    assert r["p"] is not None and r["p"] < 1e-10


def test_paired_symmetric_when_equally_good():
    """两臂精度相当 → winrate ≈ 0.5 且 p 不显著(不得凭噪声宣布胜负)。"""
    rng = np.random.default_rng(3)
    n = 400
    idx = [f"T{i}" for i in range(n)]
    a = pd.Series(np.exp(rng.normal(16, 2, n)), index=idx)
    x = a * np.exp(rng.normal(0, .25, n))
    y = a * np.exp(rng.normal(0, .25, n))
    r = _paired(x, y, a)
    assert 0.4 < r["winrate"] < 0.6, r
    assert r["p"] > 0.05, r


def test_paired_identical_arms_gives_no_p():
    """两臂逐位相同(blend 开启后 ref=prod 的自比自)→ 差分全 0,不得报 p。

    这正是事故期 AB 表的形态: rnn_* 与 prod_* 完全一致、rnn_wins 恒 False。
    Wilcoxon 在全零差分上会抛异常,必须显式返回 None 而不是崩掉整条 AB 记账。
    """
    rng = np.random.default_rng(5)
    n = 200
    idx = [f"T{i}" for i in range(n)]
    a = pd.Series(np.exp(rng.normal(16, 2, n)), index=idx)
    x = a * np.exp(rng.normal(0, .3, n))
    r = _paired(x, x.copy(), a)
    assert r["n"] == n
    assert r["winrate"] == 0.0        # 无一票严格更优
    assert r["p"] is None, "全零差分不得产生 p 值"


def test_paired_guards_bad_inputs():
    """缺臂 / 样本不足 / 非正值 → 安静降级为 None,绝不抛。"""
    rng = np.random.default_rng(9)
    idx = [f"T{i}" for i in range(100)]
    a = pd.Series(np.exp(rng.normal(16, 2, 100)), index=idx)
    x = a * 1.1
    assert _paired(None, x, a)["winrate"] is None
    assert _paired(x, None, a)["winrate"] is None
    assert _paired(x.iloc[:5], x.iloc[:5], a)["winrate"] is None   # n < min_n
    bad = x.copy()
    bad.iloc[:] = -1.0                                  # 全非正
    assert _paired(bad, x, a)["winrate"] is None


def test_paired_uses_only_common_tickers():
    """三方取交:任一方缺票不得错位对齐(错位=拿 A 票的预测比 B 票的真值)。"""
    rng = np.random.default_rng(13)
    idx = [f"T{i}" for i in range(200)]
    a = pd.Series(np.exp(rng.normal(16, 2, 200)), index=idx)
    x = (a * 1.05).iloc[:150]
    y = (a * 1.20).iloc[50:]
    r = _paired(x, y, a)
    assert r["n"] == 100, f"交集应为 T50..T149,实得 {r['n']}"
    assert r["winrate"] == 1.0        # x 处处更接近
