"""blend3 路由门(E10 议题③②,2026-08-15):max(ADV̂, 近 N 日实测 ADV)。"""
import pandas as pd

from VolumePrediction.blend_routing import rnn_layer, recent_measured_adv


def _cov(n=10):
    return pd.Index([f"T{i}" for i in range(n)])


def test_pred_only_gate_is_top_half_plus_held():
    cov = _cov()
    pred = pd.Series(range(10, 0, -1), index=cov, dtype=float)  # T0 最流动
    layer, diag = rnn_layer(cov, pred, held={"T9"}, measured_adv=None)
    assert diag["gate_source"] == "pred_only(degraded)"
    # 中位=5.5 → liq≥5.5 = T0..T4;T9 走持仓通道
    assert {"T0", "T1", "T2", "T3", "T4"} <= layer
    assert "T9" in layer                                    # 持仓票必进
    assert "T5" not in layer and "T8" not in layer


def test_measured_adv_rescues_underestimated_ticker():
    """XHG 型: 预测 $1.8k 但实测 $376M —— max() 后过中位门,不再自我固化。"""
    cov = _cov()
    pred = pd.Series([float(10 - i) * 1e6 for i in range(9)] + [1.8e3],
                     index=cov)                            # T9 被严重低估
    meas = pd.Series({"T9": 3.76e8})                       # 实测远超全场
    lo, _ = rnn_layer(cov, pred, held=set(), measured_adv=None)
    hi, diag = rnn_layer(cov, pred, held=set(), measured_adv=meas)
    assert "T9" not in lo
    assert "T9" in hi
    assert diag["gate_source"] == "max(pred,measured)"
    assert diag["n_measured_lifted"] >= 1


def test_regime_shift_visible_next_day():
    """recent_measured_adv 取 max(中位, 最近一日): XHG 8/13 单日爆量,
    8/13 晚间路由(pred_date=8/13)就必须看见,不等中位数过半。"""
    class FakeSvc:
        def _raw_dates(self):
            return ["2026-08-09", "2026-08-10", "2026-08-11",
                    "2026-08-12", "2026-08-13"]
        def _load_day(self, d):
            v = 3.76e8 if d == "2026-08-13" else 3.4e3
            return pd.DataFrame({"ticker": ["XHG"], "dollar_volume": [v]})
    s = recent_measured_adv("2026-08-13", n_days=5, svc=FakeSvc())
    assert float(s.loc["XHG"]) == 3.76e8


def test_measured_missing_tickers_fall_back_to_pred():
    cov = _cov(4)
    pred = pd.Series([4.0, 3.0, 2.0, 1.0], index=cov)
    meas = pd.Series({"T3": 100.0})                        # 只有一票有实测
    layer, _ = rnn_layer(cov, pred, held=set(), measured_adv=meas)
    assert "T3" in layer and "T0" in layer


def test_recent_measured_adv_is_pit_safe():
    """只取 ≤ pred_date 的 raw 交易日(逐日断言,防未来数据渗入)。"""
    class FakeSvc:
        def _raw_dates(self):
            return ["2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
        def _load_day(self, d):
            assert d <= "2026-08-13", f"future day {d} leaked past pred_date"
            return pd.DataFrame({"ticker": ["A"], "dollar_volume": [1.0]})
    s = recent_measured_adv("2026-08-13", n_days=3, svc=FakeSvc())
    assert float(s.loc["A"]) == 1.0


def test_recent_measured_adv_degrades_to_empty_on_error():
    class BrokenSvc:
        def _raw_dates(self):
            raise RuntimeError("no raw store")
    s = recent_measured_adv("2026-08-13", svc=BrokenSvc())
    assert s.empty


def test_full_coverage_promote_takes_all_covered():
    # RNN promote(用户终判 2026-08-17): full_coverage=True → 层=全覆盖,
    # 流动性门退役;lgbm 只守 RNN 覆盖外残尾。
    cov = pd.Index(["A", "B", "C", "D"])
    pv = pd.Series({"A": 1e9, "B": 1e6, "C": 1e3, "D": 10.0})
    layer, diag = rnn_layer(cov, pv, held={"D"}, full_coverage=True)
    assert layer == {"A", "B", "C", "D"}
    assert diag["gate_source"] == "full_coverage(promote_20260817)"
    assert diag["n_layer"] == 4 and diag["n_covered"] == 4


def test_full_coverage_default_off_keeps_layered_gate():
    # 回滚保障: 不传/False 时旧门语义逐位不变(top50% ∪ held)。
    cov = pd.Index(["A", "B", "C", "D"])
    pv = pd.Series({"A": 1e9, "B": 1e6, "C": 1e3, "D": 10.0})
    layer, _ = rnn_layer(cov, pv, held={"D"})
    assert layer == {"A", "B", "D"}
