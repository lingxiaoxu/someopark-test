"""XBRL YTD-decumulation 修复的单元测试(无网络、无磁盘)。

2026-09-11:_standalone_quarters 改为按财年起点锚定 YTD 链(取代按 SEC ``fy``
分桶),并在 _raw_quarterly_capex 加 60–110 天守卫。六个用例全部手搭事实;
对修复前的代码 6 挂(5 AssertionError + 1 KeyError),对修复后 6 过。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for p in (str(_ROOT), str(_ROOT / "qlib-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

from electric_utilities_strategy.data import company_signals as comp  # noqa: E402

MN = 1_000_000


def _facts(concept, rows):
    """rows = (start, end, val_mn, fy, fp, filed, form)"""
    units = [{"start": s, "end": e, "val": v * MN, "fy": fy, "fp": fp,
              "filed": f, "form": form} for s, e, v, fy, fp, f, form in rows]
    return {"facts": {"us-gaap": {concept: {"units": {"USD": units}}}}}


C = "PaymentsToAcquirePropertyPlantAndEquipment"


def test_cross_fy_annual_is_chained_not_phantom():
    """SO 2015: the three 10-Q YTDs carry fy=2016, the 10-K FY fact fy=2015.
    Grouping by ``fy`` orphans the annual figure and Q4 comes out as the whole
    year (5674 over 364 days).  Anchoring on the fiscal-year start instead
    chains all four rungs."""
    f = _facts(C, [
        ("2015-01-01", "2015-03-31", 1091, 2016, "Q1", "2016-05-05", "10-Q"),
        ("2015-01-01", "2015-06-30", 2239, 2016, "Q2", "2016-08-08", "10-Q"),
        ("2015-01-01", "2015-09-30", 3490, 2016, "Q3", "2016-11-04", "10-Q"),
        ("2015-01-01", "2015-12-31", 5674, 2015, "FY", "2016-02-26", "10-K"),
    ])
    q = comp._standalone_quarters(f, C)
    assert q["2015-12-31"]["days"] == 92
    assert q["2015-12-31"]["val"] == 2184 * MN
    assert q["2015-03-31"]["val"] == 1091 * MN
    assert q["2015-06-30"]["val"] == 1148 * MN
    assert q["2015-09-30"]["val"] == 1251 * MN
    assert all(60 <= r["days"] <= 110 for r in q.values())
    assert set(comp._raw_quarterly_capex(f, C)) == {
        "CY2015Q1", "CY2015Q2", "CY2015Q3", "CY2015Q4"}


def test_ttm_facts_are_not_fiscal_year_anchors():
    """AMZN tags a YTD ladder, standalone quarters AND trailing-12-month facts.
    2017-07-01 starts an explicitly-tagged standalone Q3 and a TTM; anchoring
    there yields 13035-3074 = 9961 over 273 days for 2018-06-30."""
    f = _facts("PaymentsToAcquireProductiveAssets", [
        ("2017-01-01", "2017-09-30",  8336, 2018, "Q3", "2018-10-26", "10-Q"),
        ("2017-01-01", "2017-12-31", 11955, 2018, "FY", "2019-02-01", "10-K"),
        ("2017-07-01", "2017-09-30",  3074, 2018, "Q3", "2018-10-26", "10-Q"),
        ("2017-07-01", "2018-06-30", 13035, 2019, "Q2", "2019-07-26", "10-Q"),
        ("2018-01-01", "2018-03-31",  3098, 2019, "Q1", "2019-04-26", "10-Q"),
        ("2018-01-01", "2018-06-30",  6341, 2019, "Q2", "2019-07-26", "10-Q"),
        ("2018-04-01", "2018-06-30",  3243, 2019, "Q2", "2019-07-26", "10-Q"),
    ])
    q = comp._standalone_quarters(f, "PaymentsToAcquireProductiveAssets")
    assert q["2018-06-30"]["val"] == 3243 * MN and q["2018-06-30"]["days"] == 91
    assert q["2018-03-31"]["val"] == 3098 * MN
    # 2017Q4 is recoverable by decumulation (11955-8336 over 92 days)
    assert q["2017-12-31"]["val"] == 3619 * MN and q["2017-12-31"]["days"] == 92
    # 2017Q3's ladder is truncated (no Q1/Q2 rung); the filer's own standalone
    # fact fills it rather than a 272-day artefact reaching the group layer.
    assert q["2017-09-30"]["val"] == 3074 * MN and q["2017-09-30"]["source"] == "tagged"


def test_missing_ytd_link_yields_no_quarter():
    """AWK 2014: Q1 and Q2 YTDs exist, Q3 does not, then the 10-K lands.  The
    only honest answer for Q4 is *nothing* — never FY-minus-Q2 over 184 days."""
    f = _facts(C, [
        ("2014-01-01", "2014-03-31", 192, 2014, "Q1", "2014-05-07", "10-Q"),
        ("2014-01-01", "2014-06-30", 401, 2014, "Q2", "2014-08-06", "10-Q"),
        ("2014-01-01", "2014-12-31", 956, 2016, "FY", "2017-02-21", "10-K"),
    ])
    frames = comp._raw_quarterly_capex(f, C)
    assert set(frames) == {"CY2014Q1", "CY2014Q2"}
    assert "CY2014Q4" not in frames
    q = comp._standalone_quarters(f, C)
    assert q["2014-12-31"]["days"] == 184      # visible to the guard, not to production


def test_non_calendar_fiscal_year_chains_across_fy_buckets():
    """MSFT's fiscal year ends 30 June.  Its Q2 YTD rung (end 2011-12-31) is
    labelled fy=2013 while the rest of the ladder is fy=2012, so the fy bucket
    jumps Q1 -> Q3 and calendar 2012Q1 came out as 1247 over 183 days."""
    f = _facts(C, [
        ("2011-07-01", "2011-09-30",  436, 2012, "Q1", "2011-10-20", "10-Q"),
        ("2011-07-01", "2011-12-31",  934, 2013, "Q2", "2013-01-24", "10-Q"),
        ("2011-07-01", "2012-03-31", 1683, 2012, "Q3", "2012-04-19", "10-Q"),
        ("2011-07-01", "2012-06-30", 2305, 2012, "FY", "2012-07-26", "10-K"),
    ])
    q = comp._standalone_quarters(f, C)
    assert q["2012-03-31"]["val"] == 749 * MN and q["2012-03-31"]["days"] == 91
    assert q["2011-09-30"]["days"] == 91
    assert q["2011-12-31"]["days"] == 92
    assert q["2012-06-30"]["days"] == 91
    assert set(comp._raw_quarterly_capex(f, C)) == {
        "CY2011Q3", "CY2011Q4", "CY2012Q1", "CY2012Q2"}


def test_off_boundary_starts_join_the_chain():
    """SO 2013 tagged its Q2/Q3 YTDs from 2012-12-29 / 2012-12-30.  Strict
    start-equality drops both rungs and leaves a 275-day Q4."""
    f = _facts(C, [
        ("2013-01-01", "2013-03-31", 1197, 2013, "Q1", "2013-05-10", "10-Q"),
        ("2012-12-29", "2013-06-30", 2597, 2013, "Q2", "2013-08-06", "10-Q"),
        ("2012-12-30", "2013-09-30", 3978, 2013, "Q3", "2013-11-06", "10-Q"),
        ("2013-01-01", "2013-12-31", 5463, 2013, "FY", "2014-02-27", "10-K"),
    ])
    q = comp._standalone_quarters(f, C)
    assert [q[e]["days"] for e in sorted(q)] == [89, 91, 92, 92]
    assert q["2013-12-31"]["val"] == 1485 * MN


def test_annual_only_filer_produces_nothing():
    """A filer that tags only the fiscal year (WTRG's recent capex) must yield
    no quarters at all — not one 365-day 'Q4'."""
    f = _facts(C, [
        ("2007-01-01", "2007-12-31", 232, 2011, "FY", "2012-02-27", "10-K"),
    ])
    assert comp._raw_quarterly_capex(f, C) == {}
    assert comp._standalone_quarters(f, C)["2007-12-31"]["days"] == 364




def test_guard_drops_chain_head_but_keeps_in_range_successor():
    """GOOGL 2014 形状:链头(Q3 YTD,更早的级 companyfacts 根本不收)是 272 天的
    残渣,必须被 _raw_quarterly_capex 的守卫丢弃;它在范围内的后继 Q4 保留。
    现有两套 test_xbrl_decumulation.py 都碰不到 _raw_quarterly_capex。"""
    f = _facts(C, [
        ("2014-01-01", "2014-09-30",  8000, 2014, "Q3", "2014-10-24", "10-Q"),
        ("2014-01-01", "2014-12-31", 11000, 2014, "FY", "2015-02-09", "10-K"),
    ])
    q = comp._standalone_quarters(f, C)
    assert q["2014-09-30"]["days"] == 272                       # 原语暴露残渣
    assert q["2014-12-31"]["days"] == 92 and q["2014-12-31"]["val"] == 3000 * MN
    frames = comp._raw_quarterly_capex(f, C)
    assert "CY2014Q3" not in frames                            # 守卫丢弃
    assert frames["CY2014Q4"]["val"] == 3000 * MN              # 后继保留
