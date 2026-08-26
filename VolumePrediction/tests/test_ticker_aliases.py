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

# ══════════════════ 退市(2026-08-26;AVB 并购摘牌实证)══════════════════
# AVB 2026-08-18 并购交割摘牌,最后一根 bar 2026-08-14。此后 8 天 VP 仍为它
# 报出 997,536 股的"健康" ADV —— trailing 窗口一直命中死前旧 bar,而下游三
# 个消费点只过滤 None/nan/非正数,正数一律放行。以下不变量锁死这类事故:
#   1. 退市记录与别名同规矩: 日期窗口 + 身份锚 + 回收守卫(绝不前视判死);
#   2. 判定顺序 改名优先于退市(改名票在 reference 里同样 active=false);
#   3. 两个块共用一个文件,任一写入点都不许抹掉另一块;
#   4. ADV 陈旧守卫**不依赖**登记处 —— 停牌/断源/尚未查证的退市都要拦住。

DELISTED_FIXTURE = {
    "AVB": {"delisted": "2026-08-18", "name": "AvalonBay Communities, Inc.",
            "cik": "0000915912", "figi": "BBG000BLPBL5",
            "successor_candidates": [], "verified": "polygon_reference"},
}


@pytest.fixture()
def fake_registry(monkeypatch, tmp_path):
    """别名 + 退市 双块影子登记处(绝不碰生产 ticker_aliases.json)。"""
    p = tmp_path / "ticker_aliases.json"
    p.write_text(json.dumps({"schema": "v2", "aliases": {
        "BK": {"current": "BNY", "changed": "2026-05-21",
               "cik": "0001390777", "verified": "polygon_events"},
    }, "delistings": DELISTED_FIXTURE}))
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={}, delist={})
    yield p
    ta._CACHE.update(mtime=None, data={}, delist={})


def test_delisting_is_date_windowed(fake_registry):
    """PIT 红线: 退市前该票**完全正常** —— 拿"后来退市了"去判死历史是前视。"""
    assert ta.delisting_of("AVB", "2026-08-14") is None      # 最后交易日,活的
    assert ta.delisting_of("AVB", "2026-08-17") is None      # 摘牌前一日,活的
    assert ta.delisting_of("AVB", "2026-08-18")["delisted"] == "2026-08-18"
    assert ta.is_delisted("AVB", "2026-08-26") is True
    assert ta.is_delisted("AAPL", "2026-08-26") is False


def test_delisted_name_recycled_stops(fake_registry):
    """名字被新实体取用后必须止步 —— 否则新公司一直背着旧公司的死亡记录
    (与 FB 回收同一条红线)。"""
    al = json.loads(fake_registry.read_text())
    al["delistings"]["AVB"]["recycled"] = "2028-01-10"
    fake_registry.write_text(json.dumps(al))
    ta._CACHE.update(mtime=None, data={}, delist={})
    assert ta.is_delisted("AVB", "2027-06-01") is True        # 空悬期,仍是死的
    assert ta.delisting_of("AVB", "2028-01-10") is None       # 新实体启用日起止步
    assert ta.delisting_of("AVB", "2029-05-05") is None


def test_describe_reports_both_classes_and_omits_healthy(fake_registry):
    d = ta.describe(["AVB", "BK", "AAPL", None, "AVB"], "2026-08-26")
    assert d["AVB"]["status"] == "delisted" and d["AVB"]["delisted"] == "2026-08-18"
    assert d["BK"] == {"status": "renamed", "current": "BNY"}
    assert "AAPL" not in d                                    # 健康票不出现


def test_save_never_drops_the_other_block(fake_registry):
    """v1 的两个写入点只落 aliases,加 delistings 后若不走 _save 会静默抹掉
    整个退市册。两个方向都锁。"""
    ta._save(aliases={**ta.load_aliases(), "FOO": {"current": "BAR",
                                                   "changed": "2026-01-01"}})
    ta._CACHE.update(mtime=None, data={}, delist={})
    assert "AVB" in ta.load_delistings(), "写 aliases 抹掉了 delistings"
    assert "FOO" in ta.load_aliases()

    ta._save(delistings={**ta.load_delistings(), "ZZZ": {"delisted": "2026-02-02"}})
    ta._CACHE.update(mtime=None, data={}, delist={})
    assert "BK" in ta.load_aliases() and "FOO" in ta.load_aliases(), \
        "写 delistings 抹掉了 aliases"
    assert {"AVB", "ZZZ"} <= set(ta.load_delistings())
    assert json.loads(fake_registry.read_text())["schema"] == "v2"


def test_classify_gone_prefers_rename_over_delisting(monkeypatch, tmp_path):
    """判定顺序红线: 改名票在 reference 里**同样是 active=false**(BK 实证)。
    先查 active 会把每一次改名都误判成退市 —— 必须 events 优先。"""
    p = tmp_path / "ticker_aliases.json"
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={}, delist={})
    monkeypatch.setattr(ta, "_events", lambda s, id_, k: {
        "name": "Bank of New York Mellon", "cik": "0001390777",
        "composite_figi": "BBG000BD8PN9", "events": [
            {"type": "ticker_change", "date": "2007-07-02",
             "ticker_change": {"ticker": "BK"}},
            {"type": "ticker_change", "date": "2026-05-21",
             "ticker_change": {"ticker": "BNY"}}]})

    def _boom(*a, **k):                       # 退市路径一旦被走到就炸
        raise AssertionError("改名票不该走退市判定")
    monkeypatch.setattr(ta, "_reference_row", _boom)
    r = ta.classify_gone(["BK"], api_key="stub")
    assert r["renamed"]["BK"]["current"] == "BNY"
    assert r["delisted"] == {} and r["unresolved"] == []
    ta._CACHE.update(mtime=None, data={}, delist={})
    assert "BK" not in ta.load_delistings()


def test_classify_gone_records_delisting_and_stops_requerying(monkeypatch, tmp_path):
    """AVB 型: 无改名事件 → reference(active=false) 确证退市 → 入册。
    入册后第二次调用必须**完全不发网络请求**(AVB 此前天天重查的根因)。"""
    p = tmp_path / "ticker_aliases.json"
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={}, delist={})
    monkeypatch.setattr(ta, "_events", lambda s, id_, k: None)   # 无改名事件
    monkeypatch.setattr(ta, "_master_ids", lambda t: [])
    calls = []
    monkeypatch.setattr(ta, "_reference_row", lambda s, t, k, active: (
        calls.append(t) or {"ticker": "AVB", "name": "AvalonBay Communities, Inc.",
                            "cik": "0000915912", "composite_figi": "BBG000BLPBL5",
                            "primary_exchange": "XNYS",
                            "delisted_utc": "2026-08-18T00:00:00Z"}))
    monkeypatch.setattr(ta, "_active_under_cik", lambda s, c, k: [])
    r = ta.classify_gone(["AVB"], api_key="stub")
    assert r["delisted"]["AVB"]["delisted"] == "2026-08-18"
    assert r["delisted"]["AVB"]["successor_candidates"] == []
    assert r["renamed"] == {} and r["unresolved"] == []
    assert len(calls) == 1

    ta._CACHE.update(mtime=None, data={}, delist={})
    r2 = ta.classify_gone(["AVB"], api_key="stub")               # 已入册
    assert len(calls) == 1, "已入册的票仍在重查 Polygon"
    assert r2 == {"renamed": {}, "delisted": {}, "unresolved": []}


def test_classify_gone_leaves_unverifiable_unresolved(monkeypatch, tmp_path):
    """既非改名也无摘牌日 → 存疑(停牌/断源),**不得**臆断为退市。"""
    p = tmp_path / "ticker_aliases.json"
    monkeypatch.setattr(ta, "ALIAS_PATH", p)
    ta._CACHE.update(mtime=None, data={}, delist={})
    monkeypatch.setattr(ta, "_events", lambda s, id_, k: None)
    monkeypatch.setattr(ta, "_master_ids", lambda t: [])
    monkeypatch.setattr(ta, "_reference_row", lambda s, t, k, active: None)
    r = ta.classify_gone(["HALTED"], api_key="stub")
    assert r["unresolved"] == ["HALTED"]
    assert r["delisted"] == {}
    ta._CACHE.update(mtime=None, data={}, delist={})
    assert ta.load_delistings() == {}


# ── ADV 陈旧守卫(service._Adv.info)──────────────────────────────────────

@pytest.fixture()
def synth_service(tmp_path):
    """合成 raw: LIVE 每天都印,DEAD 只印到 08-14(AVB 形态)。
    零生产依赖、零网络,artifacts 目录为空 → forecast 走 ma5 回退,
    正是 fallback_trailing 那条被守卫的路径。"""
    raw = tmp_path / "raw"
    raw.mkdir()
    days = [d.strftime("%Y-%m-%d") for d in
            pd.bdate_range("2026-07-20", "2026-08-26")]
    for d in days:
        rows = [{"ticker": "LIVE", "v": 1_000_000.0, "vw": 50.0, "c": 50.0}]
        if d <= "2026-08-14":
            rows.append({"ticker": "DEAD", "v": 900_000.0, "vw": 180.0, "c": 184.06})
        pd.DataFrame(rows).to_parquet(raw / f"grouped_{d}.parquet")
    from VolumePrediction.service import VolumeService
    return VolumeService(artifacts_dir=tmp_path / "art", raw_dir=raw), days


def test_adv_guard_blocks_stale_ticker(synth_service):
    svc, _ = synth_service
    i = svc.adv.info("DEAD", date="2026-08-26")
    assert i["adv_shares"] is None, "退市票仍给出 ADV(守卫失效)"
    assert i["source"] == "stale"
    assert i["last_bar"] == "2026-08-14" and i["stale_td"] == 8
    # 下游拿到的必须是 nan —— RiskManager/_vp_adv_forecast 的 isfinite 才拦得住
    v = svc.adv.get_adv_forecast("DEAD", date="2026-08-26")
    assert v != v, "get_adv_forecast 未退化为 NaN"
    assert svc.execute.days_to_liquidate("DEAD", 1000, date="2026-08-26")["days"] is None
    assert svc.execute.participation_cap("DEAD", date="2026-08-26")["max_shares_per_day"] is None


def test_adv_guard_silent_for_live_ticker(synth_service):
    """活票一分不能受影响(守卫误伤=全市场 sizing 断流)。"""
    svc, _ = synth_service
    i = svc.adv.info("LIVE", date="2026-08-26")
    assert i["source"] == "fallback_trailing"
    assert i["adv_shares"] == pytest.approx(1_000_000.0)


def test_adv_guard_is_point_in_time(synth_service):
    """PIT: 死前的每一天都必须照常给数;阈值内的短空档也不误杀。"""
    svc, _ = synth_service
    for d in ("2026-08-10", "2026-08-14", "2026-08-17", "2026-08-19"):
        i = svc.adv.info("DEAD", date=d)
        assert i["adv_shares"] is not None, f"{d} 被前视判死"
        assert i["source"] == "fallback_trailing"
    assert svc.adv.info("DEAD", date="2026-08-20")["source"] == "stale"


def test_adv_guard_annotates_reason_when_registry_knows(synth_service, fake_registry):
    """登记处已确证 → 标注原因;但守卫本身不依赖登记处(上面几个用例
    在空登记处下同样生效),停牌与未查证的退市一样拦得住。"""
    svc, _ = synth_service
    al = json.loads(fake_registry.read_text())
    al["delistings"]["DEAD"] = {"delisted": "2026-08-18", "name": "Dead Co"}
    fake_registry.write_text(json.dumps(al))
    ta._CACHE.update(mtime=None, data={}, delist={})
    i = svc.adv.info("DEAD", date="2026-08-26")
    assert i["corporate_action"] == "delisted" and i["delisted"] == "2026-08-18"
