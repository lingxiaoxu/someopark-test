"""TCA fills 回捞: impact 可测性判定 + participation 口径(E2,2026-08-04)。

守住的核心事实: MRPT/MTFS 是按决策价记仓的纸面账本(账面开仓价 ≡ 信号价),
没有独立成交价 → impact 在结构上不可测。若哪天有人给 pairs 塞了个
"成交价"(比如次日收盘),这组测试会失败 —— 那测的是市场漂移不是冲击。
"""
import numpy as np
import pandas as pd
import pytest

from VolumePrediction import tca_backfill as tb


class _Svc:
    """最小桩: 两天 raw,含 c 与 dollar_volume。"""

    def _raw_dates(self):
        return ["2026-06-01", "2026-06-02"]

    def _load_day(self, d):
        px = {"2026-06-01": 100.0, "2026-06-02": 110.0}[d]
        return pd.DataFrame({"ticker": ["AAA"], "c": [px],
                             "dollar_volume": [1_000_000.0], "date": [d]})


def test_impact_measurable_only_for_ledger():
    evs = [
        {"strategy": "aiss", "ticker": "AAA", "date": "2026-06-02",
         "shares": 100, "price": 110.0, "side": "BUY", "src": "ledger"},
        {"strategy": "mtfs", "ticker": "AAA", "date": "2026-06-02",
         "shares": 100, "price": 110.0, "side": "BUY", "src": "signals"},
    ]
    rows = tb._enrich(evs, _Svc())
    assert len(rows) == 2
    led = next(r for r in rows if r["src"] == "ledger")
    sig = next(r for r in rows if r["src"] == "signals")
    # 账本: 成交 110 vs 到达 100 → BUY 冲击 +10%
    assert led["impact_measurable"] is True
    assert abs(led["impact"] - 0.10) < 1e-12
    # 纸面账本: 显式留空,而不是写 0(写 0 会被当成"零冲击"污染回归)
    assert sig["impact_measurable"] is False
    assert sig["impact"] is None


def test_participation_and_sign():
    evs = [
        {"strategy": "aiss", "ticker": "AAA", "date": "2026-06-02",
         "shares": -100, "price": 110.0, "side": "SELL", "src": "ledger"},
    ]
    r = tb._enrich(evs, _Svc())[0]
    # 参与率 = 11,000 / 1,000,000
    assert abs(r["participation"] - 0.011) < 1e-12
    # SELL 在高于到达价成交 = 有利 → 冲击为负
    assert r["impact"] < 0


def test_dividends_and_fees_excluded(tmp_path, monkeypatch):
    led = tmp_path / "trade_ledger_x.jsonl"
    led.write_text("\n".join([
        '{"date":"2026-06-02","ticker":"AAA","side":"BUY","shares":10,"price":100}',
        '{"date":"2026-06-02","ticker":"AAA","side":"DIV","shares":10,"price":0.5}',
        '{"date":"2026-06-02","ticker":"AAA","side":"FEE","shares":0,"price":1}',
    ]))
    monkeypatch.setattr(tb, "LEDGERS", {"x": led})
    evs = tb._from_ledgers()
    assert len(evs) == 1 and evs[0]["side"] == "BUY"
