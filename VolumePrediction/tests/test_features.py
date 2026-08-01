"""
tests/test_features — features 层单测(小合成数据,毫秒级;临时文件仅 /tmp/vp_tests/features/)
覆盖: A6-A16 每个函数 + 双填充协议 + zscore 防前视 + lookahead_audit 正反用例。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from VolumePrediction.features import pipeline as fp                    # noqa: E402
from VolumePrediction.features import text_features, intraday_features  # noqa: E402
from VolumePrediction.data import export as pexport                     # noqa: E402
from VolumePrediction.evaluation import lookahead_audit as la           # noqa: E402

TMP = Path("/tmp/vp_tests/features")
TMP.mkdir(parents=True, exist_ok=True)


# ── 合成面板工具 ─────────────────────────────────────────────────────────────

def make_panel(n_days=300, tickers=("AAA", "BBB"), start="2019-01-02", seed=3):
    """NYSE 真实交易日 + 随机量价(可复现)。"""
    import pandas_market_calendars as mcal
    sched = mcal.get_calendar("NYSE").schedule(start_date=start, end_date="2020-12-31")
    dates = pd.DatetimeIndex(sched.index[:n_days]).normalize()
    rng = np.random.default_rng(seed)
    frames = []
    for tk in tickers:
        v_shares = rng.lognormal(15, 0.5, n_days)
        vw = rng.lognormal(4, 0.1, n_days)
        close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n_days))
        frames.append(pd.DataFrame({
            "V": v_shares * vw, "close": close,
        }, index=pd.MultiIndex.from_product([dates, [tk]], names=["date", "ticker"])))
    return pd.concat(frames).sort_index()


# ── add_volume_features ─────────────────────────────────────────────────────

class TestVolumeFeatures:
    def test_v_and_eta_definitions(self):
        p = fp.add_volume_features(make_panel())
        sub = p.xs("AAA", level="ticker")
        # v = log V
        assert np.allclose(sub["v"], np.log(sub["V"]))
        # ma5_v 前5日均 **不含当日**(论文精确定义)
        manual = sub["v"].shift(1).rolling(5, min_periods=5).mean()
        m = manual.notna()
        assert np.allclose(sub["ma5_v"][m], manual[m])
        assert sub["ma5_v"].iloc[:5].isna().all()      # 首5行无基线
        assert np.allclose(sub["eta"][m], (sub["v"] - sub["ma5_v"])[m])

    def test_zero_volume_gives_nan_not_crash(self):
        p = make_panel(n_days=30)
        p.iloc[3, p.columns.get_loc("V")] = 0.0
        out = fp.add_volume_features(p)
        assert np.isnan(out["v"].iloc[3])

    def test_requires_sorted_multiindex(self):
        p = make_panel(n_days=20)
        with pytest.raises(ValueError):
            fp.add_volume_features(p.iloc[::-1])


# ── add_return_rollups ──────────────────────────────────────────────────────

class TestReturnRollups:
    def test_eight_tech_columns_window_semantics(self):
        p = fp.add_return_rollups(fp.add_volume_features(make_panel()))
        tech = [c for c in p.columns if c.startswith("tech_")]
        assert sorted(tech) == sorted(
            [f"tech_ret_ma{w}" for w in (1, 5, 22, 252)]
            + [f"tech_v_ma{w}" for w in (1, 5, 22, 252)])
        sub = p.xs("AAA", level="ticker")
        # 未移位前: 窗口止于当日 → ma1 即自身
        m = sub["ret"].notna()
        assert np.allclose(sub["tech_ret_ma1"][m], sub["ret"][m])
        manual5 = sub["v"].rolling(5, min_periods=5).mean()
        m5 = manual5.notna()
        assert np.allclose(sub["tech_v_ma5"][m5], manual5[m5])

    def test_ret_computed_from_close_when_missing(self):
        p = fp.add_return_rollups(fp.add_volume_features(make_panel()))
        sub = p.xs("BBB", level="ticker")
        manual = sub["close"].pct_change()
        m = manual.notna()
        assert np.allclose(sub["ret"][m], manual[m])


# ── create_earnings_dummies ─────────────────────────────────────────────────

class TestEarningsDummies:
    def _panel_dates(self, p):
        return p.xs("AAA", level="ticker").index

    def test_bucket_offsets_exact(self):
        p = make_panel(n_days=60, tickers=("AAA",))
        dates = self._panel_dates(p)
        ann = dates[30]                                 # 第 31 个交易日发财报
        out = fp.create_earnings_dummies(p, {"AAA": [ann]})
        sub = out.xs("AAA", level="ticker")
        assert sub.loc[dates[30], "earn_zero"] == 1.0            # 当日
        assert sub.loc[dates[29], "earn_pos1"] == 1.0            # 前一交易日: 距1
        assert sub.loc[dates[26], "earn_pos4"] == 1.0
        assert sub.loc[dates[25], "earn_ge_pos5"] == 1.0
        assert sub.loc[dates[31], "earn_neg1"] == 1.0            # 发布次日,无未来预定
        assert sub.loc[dates[33], "earn_neg3"] == 1.0
        assert sub.loc[dates[34], "earn_le_neg4"] == 1.0
        assert sub.loc[dates[45], "earn_le_neg4"] == 1.0
        # 每行至多一个 1(one-hot)
        earn_cols = [f"earn_{b}" for b in fp.EARN_BUCKETS]
        assert (sub[earn_cols].sum(axis=1) <= 1.0 + 1e-12).all()
        assert (sub[earn_cols].sum(axis=1) == 1.0).all()         # 有信息时恰为 1

    def test_future_calendar_merged(self):
        p = make_panel(n_days=40, tickers=("AAA",))
        dates = self._panel_dates(p)
        out = fp.create_earnings_dummies(p, {"AAA": [dates[5]]},
                                         future_dates={"AAA": [dates[35]]})
        sub = out.xs("AAA", level="ticker")
        assert sub.loc[dates[34], "earn_pos1"] == 1.0            # 未来日历生效
        assert sub.loc[dates[6], "earn_ge_pos5"] == 1.0          # 两次财报之间距下次>5

    def test_nonlisted_announcement_maps_to_next_trading_day(self):
        p = make_panel(n_days=40, tickers=("AAA",))
        dates = self._panel_dates(p)
        saturday = dates[10] + pd.Timedelta(days=(5 - dates[10].dayofweek) % 7 or 7)
        assert saturday.dayofweek >= 5 or saturday not in dates
        out = fp.create_earnings_dummies(p, {"AAA": [saturday]})
        sub = out.xs("AAA", level="ticker")
        react = dates[dates.searchsorted(saturday)]
        assert sub.loc[react, "earn_zero"] == 1.0

    def test_no_info_ticker_all_zero(self):
        p = make_panel(n_days=20)
        out = fp.create_earnings_dummies(p, {"AAA": [self._panel_dates(p)[5]]})
        sub_b = out.xs("BBB", level="ticker")
        assert (sub_b[[f"earn_{b}" for b in fp.EARN_BUCKETS]].sum(axis=1) == 0).all()


# ── add_calendar_flags(已知日期表断言)──────────────────────────────────────

class TestCalendarFlags:
    def test_known_dates_2019(self):
        p = make_panel(n_days=252, tickers=("AAA",), start="2019-01-02")
        out = fp.add_calendar_flags(p).xs("AAA", level="ticker")

        def flag(day, col):
            d = pd.Timestamp(day)
            return out.loc[d, col] if d in out.index else None

        assert flag("2019-11-29", "cal_is_early_close") == 1.0   # 感恩节次日半日市
        assert flag("2019-07-03", "cal_is_early_close") == 1.0
        assert flag("2019-09-20", "cal_triple_witching") == 1.0  # 9 月第 3 个周五
        assert flag("2019-06-21", "cal_triple_witching") == 1.0
        assert flag("2019-01-18", "cal_double_witching") == 1.0  # 1 月第 3 个周五
        assert flag("2019-06-28", "cal_russell_rebalance") == 1.0  # 6 月第 4 个周五
        # 反例
        assert flag("2019-06-21", "cal_russell_rebalance") == 0.0
        assert flag("2019-09-20", "cal_double_witching") == 0.0
        assert flag("2019-05-06", "cal_is_early_close") == 0.0


# ── 填充协议 ────────────────────────────────────────────────────────────────

class TestFillPolicies:
    def _panel_with_nans(self):
        p = fp.add_return_rollups(fp.add_volume_features(make_panel(n_days=40)))
        p.loc[(p.index[10][0], "AAA"), "tech_v_ma1"] = np.nan
        return p

    def test_paper_zero_fill(self):
        p = self._panel_with_nans()
        cols = [c for c in p.columns if c.startswith("tech_")]
        out = fp.fill(p, cols, policy="paper")
        assert out[cols].isna().sum().sum() == 0
        # 首行 tech_ret_ma252 必为 0(窗口不足 → 论文零填充)
        assert out.xs("AAA", level="ticker")["tech_ret_ma252"].iloc[0] == 0.0

    def test_legacy_past_median_no_lookahead(self):
        p = make_panel(n_days=30, tickers=("AAA",))
        p = fp.add_volume_features(p)
        col = "v"
        idx15 = p.index[15]
        truth_past_median = p[col].iloc[:15].median()      # ≤t−1
        p.loc[idx15, col] = np.nan
        out = fp.fill_with_stock_past_median(p, [col])
        assert np.isclose(out.loc[idx15, col], truth_past_median)

    def test_legacy_global_then_zero(self):
        p = make_panel(n_days=20, tickers=("AAA",))
        p["fund1_x"] = np.nan                              # 全 NaN 列
        out = fp.fill(p, ["fund1_x"], policy="legacy")
        assert (out["fund1_x"] == 0.0).all()

    def test_unknown_policy_raises(self):
        with pytest.raises(ValueError):
            fp.fill(make_panel(n_days=10), ["V"], policy="bogus")


# ── zscore_normalize ────────────────────────────────────────────────────────

class TestZscore:
    def test_train_window_stats_only(self):
        p = fp.add_volume_features(make_panel(n_days=100, tickers=("AAA",)))
        train_end = str(p.index.get_level_values("date")[60].date())
        out1 = fp.zscore_normalize(p, ["v"], train_end=train_end)
        # 篡改 train_end 之后的数据不应影响任何 z 值来源统计量 → 重算前段完全一致
        p2 = p.copy()
        mask_after = p2.index.get_level_values("date") > pd.Timestamp(train_end)
        p2.loc[mask_after, "v"] = 999.0
        out2 = fp.zscore_normalize(p2, ["v"], train_end=train_end)
        mask_before = ~mask_after
        assert np.allclose(out1.loc[mask_before, "v"], out2.loc[mask_before, "v"])

    def test_causal_mode_no_lookahead(self):
        p = fp.add_volume_features(make_panel(n_days=60, tickers=("AAA",)))
        out1 = fp.zscore_normalize(p, ["v"])
        p2 = p.copy()
        last = p2.index[-1]
        p2.loc[last, "v"] = 1e6                            # 篡改最后一天
        out2 = fp.zscore_normalize(p2, ["v"])
        # 除最后一行外全部 z 不变(expanding+shift(1) 全因果)
        assert np.allclose(out1["v"].iloc[:-1], out2["v"].iloc[:-1])


# ── A14 移位 + 前视审计 ─────────────────────────────────────────────────────

class TestShiftAndAudit:
    def _full(self, n_days=320):
        p = make_panel(n_days=n_days)
        p = fp.add_volume_features(p)
        p = fp.add_return_rollups(p)
        p = fp.add_calendar_flags(p)
        return fp.shift_volume_columns_and_drop_last(p)

    def test_shift_semantics(self):
        p = make_panel(n_days=50, tickers=("AAA",))
        p = fp.add_return_rollups(fp.add_volume_features(p))
        s = fp.shift_volume_columns_and_drop_last(p).xs("AAA", level="ticker")
        raw = fp.add_return_rollups(fp.add_volume_features(
            make_panel(n_days=50, tickers=("AAA",)))).xs("AAA", level="ticker")
        # 行 t 的 tech_v_ma1 == 原始 v(t−1)(对齐到 s 的索引再比较)
        expect = raw["v"].shift(1).reindex(s.index)
        m = s["tech_v_ma1"].notna() & expect.notna()
        assert np.allclose(s["tech_v_ma1"][m], expect[m])
        # 每票首行被丢弃
        assert len(s) == len(raw) - 1

    def test_audit_green(self):
        rep = la.audit(self._full(), n_tickers=2)
        assert rep["passed"], la.to_markdown(rep)

    def test_audit_catches_injected_lookahead(self):
        p = self._full()
        # 注入前视: tech_v_ma1 改为当日 v
        p["tech_v_ma1"] = p["v"]
        rep = la.audit(p, n_tickers=2)
        assert not rep["passed"]
        assert any("tech_v_ma1" in v for v in rep["violations"])

    def test_audit_fundamental_pit(self):
        p = self._full(n_days=60)
        dates = p.xs("AAA", level="ticker").index
        # 合规: 变化日前有 acceptedDate
        p["fund1_be_me"] = 1.0
        chg = dates[30]
        p.loc[(slice(chg, None), "AAA"), "fund1_be_me"] = 2.0
        ok_rep = la.audit(p, n_tickers=2,
                          availability_dates={"AAA": [dates[28]], "BBB": [dates[0]]})
        assert ok_rep["passed"], la.to_markdown(ok_rep)
        # 违规: 无任何早于变化日的 acceptedDate
        bad_rep = la.audit(p, n_tickers=2,
                           availability_dates={"AAA": [dates[45]], "BBB": [dates[0]]})
        assert not bad_rep["passed"]
        assert any("PIT" in v for v in bad_rep["violations"])

    def test_prove_shift_correctness(self):
        proof = la.prove_shift_correctness()
        assert proof["passed"], proof["detail"]


# ── data/export ─────────────────────────────────────────────────────────────

class TestExport:
    def test_roundtrip_and_latest_pointer(self):
        p = fp.add_volume_features(make_panel(n_days=30))
        out_dir = TMP / "panel"
        path = pexport.save_panel(p, fill_policy="paper", tag="unittest", out_dir=out_dir)
        assert path.exists() and path.parent == out_dir
        meta = pexport.latest_meta(out_dir=out_dir)
        assert meta["rows"] == len(p) and meta["fill_policy"] == "paper"
        back = pexport.load_panel(out_dir=out_dir)
        pd.testing.assert_frame_equal(back, p.sort_index())

    def test_no_write_outside_tmp_in_tests(self):
        # 本测试文件自身纪律: out_dir 全部显式指向 /tmp
        assert str(TMP).startswith("/tmp/")


# ── 推迟占位模块 ────────────────────────────────────────────────────────────

class TestDeferredPlaceholders:
    def test_text_features_deferred(self):
        for fn in (text_features.daily_sentiment, text_features.novelty,
                   text_features.lda_topics):
            with pytest.raises(NotImplementedError):
                fn()

    def test_intraday_deferred(self):
        with pytest.raises(NotImplementedError):
            intraday_features.intraday_shape()

    def test_no_yfinance_in_features_layer(self):
        import VolumePrediction.features.pipeline as m
        src = Path(m.__file__).read_text()
        assert "yfinance" not in src
