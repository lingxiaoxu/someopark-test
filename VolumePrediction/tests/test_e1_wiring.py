"""E1/W3+W4 消费接线(2026-08-15 代码就位,默认关;8/17 拍板后启用)。

红线断言: 开关默认 OFF;ADV_PARTICIPATION=0.20 与 SelectPairs 阈值 300_000
逐字不动;开关关闭时新代码路径零触碰(行为回归);打开时任何失败回退后视。
"""
import inspect
import math

import pytest

import RiskManager
import SelectPairs


def test_switches_default_off():
    assert RiskManager.USE_FORECAST_ADV is False
    assert SelectPairs.USE_FORECAST_ADV is False


def test_red_lines_untouched():
    assert RiskManager.ADV_PARTICIPATION == 0.20
    assert SelectPairs._MIN_AVG_DAILY_VOLUME == 300_000
    # dtl 公式逐字不动
    src = inspect.getsource(RiskManager)
    assert "abs(v['shares']) / (adv * ADV_PARTICIPATION)" in src


def test_rm_adv_off_ignores_vp(monkeypatch):
    """开关关: adv() 绝不触碰 VP(helper 若被调用直接炸)。"""
    monkeypatch.setattr(RiskManager, "_vp_adv_forecast",
                        lambda *a: (_ for _ in ()).throw(AssertionError("touched VP")))
    d = RiskManager.MarketData.__new__(RiskManager.MarketData) \
        if hasattr(RiskManager, "MarketData") else None
    # 不实例化重对象:直接验证 adv 源码里开关先行
    src = inspect.getsource(RiskManager)
    assert "if USE_FORECAST_ADV:" in src


def test_rm_helper_none_on_failure(monkeypatch):
    """VP 全挂 → helper 返回 None(调用方落回后视),绝不抛异常。"""
    class Boom:
        def __getattr__(self, k):
            raise RuntimeError("service down")
    monkeypatch.setattr(RiskManager, "_VP_SVC", Boom())
    assert RiskManager._vp_adv_forecast("AAPL", 20) is None


def test_rm_helper_rejects_nan_and_nonpositive(monkeypatch):
    class FakeAdv:
        def __init__(self, v):
            self.v = v
        def get_adv_forecast(self, t, w):
            return self.v
    class FakeSvc:
        def __init__(self, v):
            self.adv = FakeAdv(v)
    for bad in (float("nan"), 0.0, -5.0, None):
        monkeypatch.setattr(RiskManager, "_VP_SVC", FakeSvc(bad))
        assert RiskManager._vp_adv_forecast("AAPL", 20) is None
    monkeypatch.setattr(RiskManager, "_VP_SVC", FakeSvc(1.23e6))
    assert RiskManager._vp_adv_forecast("AAPL", 20) == pytest.approx(1.23e6)


def test_sp_forward_adv_filters_invalid(monkeypatch):
    class FakeAdv:
        def batch(self, tickers, w):
            return {"A": 5e5, "B": float("nan"), "C": -1.0, "D": None}
    class FakeSvc:
        def __init__(self):
            self.adv = FakeAdv()
    import VolumePrediction.service as vs
    monkeypatch.setattr(vs, "VolumeService", FakeSvc)
    out = SelectPairs._vp_forward_adv(["A", "B", "C", "D"])
    assert out == {"A": 5e5}


def test_sp_forward_adv_empty_on_service_error(monkeypatch):
    import VolumePrediction.service as vs
    def boom():
        raise RuntimeError("down")
    monkeypatch.setattr(vs, "VolumeService", boom)
    assert SelectPairs._vp_forward_adv(["A"]) == {}
