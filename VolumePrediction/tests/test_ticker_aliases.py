"""ticker_aliases 统一改名处理(2026-08-15;BK→BNY / FB 回收实证)。

核心不变量:
1. 别名带日期窗口 —— 改名日前旧名有效(历史),改名日后解析到现名(查询);
2. 回收名(FB 今为 ProShares ETF)绝不误映射;
3. VP 查询面(forecast/adv/_ticker_frame)旧名自动解析且如实标注。
"""
import json

import pandas as pd
import pytest

import ticker_aliases as ta


@pytest.fixture()
def fake_aliases(monkeypatch, tmp_path):
    p = tmp_path / "ticker_aliases.json"
    p.write_text(json.dumps({"schema": "v1", "aliases": {
        "BK": {"current": "BNY", "changed": "2026-05-21",
               "cik": "0001390777", "verified": "polygon_events"},
        # FB 全生命周期: =Meta 至 2022-06-09 改名;空窗;2025-06-26 被 ETF 回收
        "FB": {"current": "META", "changed": "2022-06-09",
               "recycled": "2025-06-26",
               "cik": "0001326801", "figi": "BBG000MM2P62",
               "verified": "polygon_events"},
    }}))
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={})
    yield
    ta._CACHE.update(mtime=None, data={})


def test_resolve_is_date_windowed(fake_aliases):
    assert ta.resolve("BK") == "BNY"                     # 现在 → 现名
    assert ta.resolve("BK", "2026-08-14") == "BNY"       # 改名后 → 现名
    assert ta.resolve("BK", "2026-01-05") == "BK"        # 改名前 → 旧名(历史)
    assert ta.resolve("BNY") == "BNY"                    # 现名恒等
    assert ta.resolve("AAPL") == "AAPL"                  # 无别名恒等


def test_recycled_name_never_mismapped(fake_aliases):
    """FB 自 2025-06-26 被 ETF 回收: 归一映射只作用于 changed 之前的日子,
    回收后当日帧里的 FB(ETF)绝不被改成 META。"""
    m_old = ta.rename_map("2022-01-03")                  # META 还叫 FB 的年代
    m_new = ta.rename_map("2026-08-14")                  # FB=ETF 的年代
    assert m_old.get("FB") == "META"
    assert "FB" not in m_new
    df = pd.DataFrame({"ticker": ["FB", "AAPL"], "v": [1.0, 2.0]})
    out_new = ta.normalize_day_frame(df, "2026-08-14")
    assert list(out_new["ticker"]) == ["FB", "AAPL"]     # ETF 原样
    out_old = ta.normalize_day_frame(df, "2022-01-03")
    assert list(out_old["ticker"]) == ["META", "AAPL"]   # 历史归一


def test_normalize_zero_cost_when_no_hit(fake_aliases):
    df = pd.DataFrame({"ticker": ["AAPL"], "v": [1.0]})
    assert ta.normalize_day_frame(df, "2022-01-03") is df   # 零匹配返回原帧


def test_entry_from_events_rejects_recycled_entity():
    """回收实体(事件链里 old 即最新名)→ None;真改名 → 条目。"""
    recycled = {"name": "ProShares ETF", "events": [
        {"type": "ticker_change", "date": "2025-06-26",
         "ticker_change": {"ticker": "FB"}}]}
    assert ta._entry_from_events(recycled, "FB") is None
    renamed = {"name": "BNY", "cik": "1", "composite_figi": "F", "events": [
        {"type": "ticker_change", "date": "2007-07-02",
         "ticker_change": {"ticker": "BK"}},
        {"type": "ticker_change", "date": "2026-05-21",
         "ticker_change": {"ticker": "BNY"}}]}
    e = ta._entry_from_events(renamed, "BK")
    assert e and e["current"] == "BNY" and e["changed"] == "2026-05-21"


def test_resolve_hop_guard(fake_aliases, monkeypatch):
    """环状别名(数据错误)不得死循环。"""
    ta._CACHE.update(mtime=object(), data={
        "A": {"current": "B", "changed": "2020-01-01"},
        "B": {"current": "A", "changed": "2020-01-02"}})
    assert ta.resolve("A") in ("A", "B")                 # 有限步返回即可
    ta._CACHE.update(mtime=None, data={})


def test_fb_full_lifecycle_resolve_matrix(fake_aliases):
    """FB=Meta(→2022-06-09)→ 空窗 → 2025-06-26 被 ETF 回收。
    resolve = 市场名语义(不可变历史工件的行名);逐窗断言。"""
    assert ta.resolve("FB", "2021-01-04") == "FB"     # Meta 时代,当日市场名=FB
    assert ta.resolve("FB", "2023-01-04") == "META"   # 空窗期,FB 只可能指 Meta
    assert ta.resolve("FB", "2025-06-25") == "META"   # 回收前最后一天
    assert ta.resolve("FB", "2025-06-26") == "FB"     # 回收日起 FB=ETF,止步
    assert ta.resolve("FB") == "FB"                   # 今天 → ETF,绝不给 META


def test_fb_full_lifecycle_canonical_matrix(fake_aliases):
    """canonical = 归一数据语义(_load_day 已把历史行归一到现名):
    改名前日期也必须给现名,否则在归一数据里查空(复审修正的 bug)。"""
    assert ta.canonical("FB", "2021-01-04") == "META"  # 归一数据里 Meta 行=META
    assert ta.canonical("FB", "2023-01-04") == "META"
    assert ta.canonical("FB", "2026-08-14") == "FB"    # ETF 归一后仍是 FB
    assert ta.canonical("FB") == "FB"
    # BK(无回收): 任何日期 canonical 都是 BNY —— 历史查询不再错位
    assert ta.canonical("BK", "2026-01-05") == "BNY"
    assert ta.canonical("BK") == "BNY"


def test_normalize_collision_guard(fake_aliases):
    """历史日里 current 名已被另一实体占用 → 拒绝归一(防重复行被
    pivot aggfunc='first' 静默吞),其余票照常归一。"""
    df = pd.DataFrame({"ticker": ["FB", "META", "BK"], "v": [1., 2., 3.]})
    out = ta.normalize_day_frame(df, "2021-01-04")     # FB→META 撞车;BK→BNY 正常
    assert list(out["ticker"]) == ["FB", "META", "BNY"]
    assert len(out) == len(set(out["ticker"])), "归一造出重复票行"


def test_recycled_date_detection():
    """回收探测: 旧名直查命中另一实体(FIGI/CIK 不同)→ 取其拿名日期;
    同实体/404 → None。"""
    meta_entry = {"figi": "BBG000MM2P62", "cik": "0001326801"}
    etf = {"composite_figi": "BBG01VRMNFB1", "cik": "0001174610", "events": [
        {"type": "ticker_change", "date": "2025-06-26",
         "ticker_change": {"ticker": "FB"}}]}
    assert ta._recycled_date(etf, meta_entry, "FB") == "2025-06-26"
    same = {"composite_figi": "BBG000MM2P62", "cik": "0001326801", "events": []}
    assert ta._recycled_date(same, meta_entry, "FB") is None
    assert ta._recycled_date(None, meta_entry, "FB") is None


def test_refresh_pipeline_detects_fb_recycle(monkeypatch, tmp_path):
    """refresh_aliases 全流程(打桩 Polygon): FB 直查=ETF 被拒 → CIK 回退
    找到 Meta 链 → 条目落盘且带 recycled=2025-06-26,零网络零生产写。"""
    p = tmp_path / "ticker_aliases.json"
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={})
    etf = {"name": "ProShares ETF", "composite_figi": "BBG01VRMNFB1",
           "cik": "0001174610", "events": [
               {"type": "ticker_change", "date": "2025-06-26",
                "ticker_change": {"ticker": "FB"}}]}
    meta = {"name": "Meta Platforms", "composite_figi": "BBG000MM2P62",
            "cik": "0001326801", "events": [
                {"type": "ticker_change", "date": "2012-05-18",
                 "ticker_change": {"ticker": "FB"}},
                {"type": "ticker_change", "date": "2022-06-09",
                 "ticker_change": {"ticker": "META"}}]}
    monkeypatch.setattr(ta, "_events",
                        lambda s, id_, k: etf if id_ == "FB" else meta)
    monkeypatch.setattr(ta, "_master_ids", lambda t: ["0001326801"])
    r = ta.refresh_aliases(["FB"], api_key="stub")
    e = r["added"]["FB"]
    assert e["current"] == "META" and e["changed"] == "2022-06-09"
    assert e["recycled"] == "2025-06-26"
    ta._CACHE.update(mtime=None, data={})
    assert ta.resolve("FB") == "FB"                    # 落盘后今天解析=ETF 原样
    assert ta.canonical("FB", "2021-01-04") == "META"
    ta._CACHE.update(mtime=None, data={})


def test_vp_ticker_frame_historical_old_name_real_data():
    """真数据(只读): _ticker_frame('BK', 改名前 end_date) 曾因 resolve/归一
    错位查空 —— canonical 修正后必须拿到(归一为 BNY 的)历史行。"""
    if "BK" not in ta.load_aliases():
        pytest.skip("production alias file lacks BK")
    from VolumePrediction.service import VolumeService
    tf = VolumeService()._ticker_frame("BK", "2026-05-14", lookback=5)
    assert len(tf) >= 3, "改名前历史查询仍为空(canonical 未生效?)"


def test_vp_service_resolves_old_name_end_to_end():
    """真实服务: 用旧名 BK 查询 → 拿到 BNY 的数据并标注 resolved_ticker。
    (依赖生产 ticker_aliases.json 已 seed BK;raw 里有 BNY。)"""
    if "BK" not in ta.load_aliases():
        pytest.skip("production alias file lacks BK")
    from VolumePrediction.service import VolumeService
    svc = VolumeService()
    f = svc.forecast.get("BK")
    assert len(f) == 1
    r = f.iloc[0]
    assert r["symbol"] == "BK"                           # 调用方原 key 保留
    if r["source"] == "model":
        assert r.get("resolved_ticker") == "BNY"
        assert pd.notna(r["pred_V"]) and r["pred_V"] > 0
    adv = svc.adv.get_adv_forecast("BK")
    assert adv == adv and adv > 0                        # 不再 NaN


def test_recheck_recycled_updates_entry(monkeypatch, tmp_path):
    """周期复查: 无主旧名(BK 型)被新实体启用后,recycled 自动补记且
    resolve 止步;7 天内不重复打 API;全程 tmp,零生产写。"""
    p = tmp_path / "ticker_aliases.json"
    p.write_text(json.dumps({"schema": "v1", "aliases": {
        "BK": {"current": "BNY", "changed": "2026-05-21",
               "figi": "BBG000BD8PN9", "cik": "0001390777"}}}))
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={})
    calls = {"n": 0}
    newco = {"name": "NewCo", "composite_figi": "BBG_NEW", "cik": "9999",
             "events": [{"type": "ticker_change", "date": "2027-03-01",
                         "ticker_change": {"ticker": "BK"}}]}

    def fake_events(s, id_, k):
        calls["n"] += 1
        return newco
    monkeypatch.setattr(ta, "_events", fake_events)
    r = ta.recheck_recycled(api_key="stub")
    assert r["recycled_found"] == {"BK": "2027-03-01"}
    ta._CACHE.update(mtime=None, data={})
    assert ta.resolve("BK", "2027-06-01") == "BK"       # 回收后止步
    assert ta.resolve("BK", "2026-08-14") == "BNY"      # 回收前照常
    # 戳记生效: 再跑一次不应再打 API
    n0 = calls["n"]
    r2 = ta.recheck_recycled(api_key="stub")
    assert calls["n"] == n0 and r2["checked"] == []
    ta._CACHE.update(mtime=None, data={})


def test_recheck_skips_when_still_unclaimed(monkeypatch, tmp_path):
    """旧名仍无主(404)→ 只更新戳记,不误标回收。"""
    p = tmp_path / "ticker_aliases.json"
    p.write_text(json.dumps({"schema": "v1", "aliases": {
        "BK": {"current": "BNY", "changed": "2026-05-21",
               "figi": "BBG000BD8PN9", "cik": "0001390777"}}}))
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={})
    monkeypatch.setattr(ta, "_events", lambda s, id_, k: None)
    r = ta.recheck_recycled(api_key="stub")
    assert r["checked"] == ["BK"] and not r["recycled_found"]
    ta._CACHE.update(mtime=None, data={})
    e = ta.load_aliases()["BK"]
    assert e.get("recycled") is None and e.get("recycled_checked")
    ta._CACHE.update(mtime=None, data={})
