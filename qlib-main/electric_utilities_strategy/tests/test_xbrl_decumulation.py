"""XBRL 单季度还原(company_signals._duration_facts / _standalone_quarters,2026-08-27)。

**为什么必须有这个测试文件**: MU 的 DIO 名义上是季频,实际一年只动一次(每年 12 月),
而所有体检都说正常。根因不在 MU 这边,而在 ``sec.concept_series`` 按 ``end`` 单键去重 ——
对 *时点* 概念(InventoryNet)这是对的,对 *区间* 概念(COGS)就是错的: 一份 10-Q 会
用**同一个 end、同一个 filed** 同时标注"财年至今"和"本季度"两个事实,end 单键只能留下
一个,而留下哪个取决于 SEC 文件里的先后顺序。MU 2026Q2 留下的是 181 天的 YTD,随后被
80-100 天过滤器丢掉 —— 于是每年只有 Q1(其 YTD 恰好等于单季)幸存。

所以本文件钉住的核心不变量是: **区间概念的主键是 (start, end),不是 end。**

沙箱纪律: 全部用手写的 facts 字典,不联网、不读写任何生产文件。
"""
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[3]              # someopark-test/
for p in (str(_ROOT), str(_ROOT / "qlib-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

from electric_utilities_strategy.data import company_signals as cs  # noqa: E402

CONCEPT = "CostOfGoodsAndServicesSold"


def _facts(items: list) -> dict:
    """把 (start, end, val_mn, fy, fp, filed) 元组包成 companyfacts 结构。"""
    return {"facts": {"us-gaap": {CONCEPT: {"units": {"USD": [
        {"start": s, "end": e, "val": v * 1e6, "fy": fy, "fp": fp,
         "filed": filed, "form": "10-Q"}
        for (s, e, v, fy, fp, filed) in items
    ]}}}}}


# MU FY2026 的真实形状(数值取自 CIK 723125 companyfacts,2026-08-27 拉取):
# Q1 只有单季;Q2/Q3 同时有 YTD 和单季,且两者 filed 完全相同。
MU_FY2026 = [
    ("2025-08-29", "2025-11-27",  5997, 2026, "Q1", "2025-12-18"),   # 90d 单季
    ("2025-08-29", "2026-02-26", 12102, 2026, "Q2", "2026-03-19"),   # 181d YTD
    ("2025-11-28", "2026-02-26",  6105, 2026, "Q2", "2026-03-19"),   # 90d 单季
    ("2025-08-29", "2026-05-28", 18502, 2026, "Q3", "2026-06-25"),   # 272d YTD
    ("2026-02-27", "2026-05-28",  6400, 2026, "Q3", "2026-06-25"),   # 90d 单季
]


# ── 事故的根因 ──────────────────────────────────────────────────────────────

def test_duration_facts_key_is_start_and_end_not_end_alone():
    """同一个 end 上的 YTD 与单季必须**同时**保留 —— 这正是 concept_series 丢掉的。"""
    d = cs._duration_facts(_facts(MU_FY2026), CONCEPT)
    same_end = {k: v for k, v in d.items() if k[1] == "2026-02-26"}
    assert len(same_end) == 2, "end=2026-02-26 上有 YTD(181d) 和单季(90d) 两个事实"
    assert sorted(v["days"] for v in same_end.values()) == [90, 181]


def test_the_incident_would_have_been_caught():
    """还原后 Q2/Q3 必须在 —— 事故表现就是它们消失、信号退化成年频。"""
    q = cs._standalone_quarters(_facts(MU_FY2026), CONCEPT)
    assert set(q) == {"2025-11-27", "2026-02-26", "2026-05-28"}


# ── 还原的正确性 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("end,expect_mn", [
    ("2025-11-27", 5997),    # Q1: YTD 即单季
    ("2026-02-26", 6105),    # Q2 = 12102 - 5997
    ("2026-05-28", 6400),    # Q3 = 18502 - 12102
])
def test_decumulation_matches_the_filers_own_quarterly_number(end, expect_mn):
    """差分值必须与 MU 自己标注的单季数**逐元**相等 —— 两条路互为验算。"""
    q = cs._standalone_quarters(_facts(MU_FY2026), CONCEPT)   # 默认纯差分
    assert q[end]["source"] == "decumulated"
    assert round(q[end]["val"] / 1e6) == expect_mn


def test_q4_comes_from_full_year_minus_ytd_q3():
    """Q4 没有独立事实,只能 FY - YTD(Q3);days 也要跟着相减。"""
    fy2025 = [   # MU FY2025 真实数值,Q4 无独立事实
        ("2024-08-30", "2024-11-28",  5361, 2025, "Q1", "2024-12-19"),
        ("2024-08-30", "2025-02-27", 10451, 2025, "Q2", "2025-03-21"),
        ("2024-08-30", "2025-05-29", 16244, 2025, "Q3", "2025-06-26"),
        ("2024-08-30", "2025-08-28", 22505, 2025, "FY", "2025-10-03"),
    ]
    q = cs._standalone_quarters(_facts(fy2025), CONCEPT)
    assert round(q["2025-08-28"]["val"] / 1e6) == 22505 - 16244 == 6261
    assert q["2025-08-28"]["days"] == 363 - 272     # 91 天,落在 60-110 合理区间


def test_availability_is_the_later_filing_of_the_pair():
    """差分要两期都在手才算得出 → 可用日 = 后一期的 filed,不是前一期。

    取前一期就等于宣称"减法在被减数公布前就做完了" —— 教科书级前视泄露。
    """
    q = cs._standalone_quarters(_facts(MU_FY2026), CONCEPT)
    assert q["2026-02-26"]["filed"] == "2026-03-19"   # YTD(Q2) 的申报日
    assert q["2026-05-28"]["filed"] == "2026-06-25"   # YTD(Q3) 的申报日


# ── prefer_tagged 开关的契约 ────────────────────────────────────────────────

def test_prefer_tagged_defaults_off_to_keep_hyperscaler_frozen():
    """默认必须是纯差分。

    打开 tagged 优先会改动 hyperscaler CapEx 的历史(补回 AMZN 缺失季,
    CY2018Q3 的 n_companies 3→4)—— 那是另一条在产信号,不能被这次改动顺手改写。
    """
    q = cs._standalone_quarters(_facts(MU_FY2026), CONCEPT)
    assert all(v["source"] == "decumulated" for v in q.values())


def test_prefer_tagged_uses_the_filers_own_fact():
    q = cs._standalone_quarters(_facts(MU_FY2026), CONCEPT, prefer_tagged=True)
    assert q["2026-02-26"]["source"] == "tagged"
    assert round(q["2026-02-26"]["val"] / 1e6) == 6105     # 与差分同值
    assert q["2026-02-26"]["days"] == 90                   # 但用的是真实 90 天


def test_prefer_tagged_recovers_a_quarter_the_fy_grouping_drops():
    """FY 分组会把某些季度漏掉,tagged 路径要能补回来。

    MU end=2009-12-03 的真实情形: 该 fy 组里还有上一年的比较期 Q1(start 更早),
    fy_start 因此解析到上一年,这条自己的 YTD 链就把自己排除了。
    """
    rows = [
        ("2008-09-05", "2008-12-04", 1200, 2010, "Q1", "2009-01-12"),  # 比较期
        ("2009-09-04", "2009-12-03", 1297, 2010, "Q1", "2010-01-12"),  # 本期
    ]
    assert "2009-12-03" not in cs._standalone_quarters(_facts(rows), CONCEPT)
    tagged = cs._standalone_quarters(_facts(rows), CONCEPT, prefer_tagged=True)
    assert round(tagged["2009-12-03"]["val"] / 1e6) == 1297


# ── PIT 去重 ────────────────────────────────────────────────────────────────

def test_restatement_keeps_the_earliest_filing():
    """同一 (start,end) 被重述时保留**最早**申报日。

    保留最晚的后果是: 该数字看起来比实际晚一年才可用(MU 有 15 个季度就是这样,
    2009-12-03 被记成 2011-01-11 而真实公布日是 2010-01-12),回测因此白等一年。
    """
    rows = [
        ("2025-11-28", "2026-02-26", 6105, 2026, "Q2", "2026-03-19"),
        ("2025-11-28", "2026-02-26", 6110, 2027, "Q2", "2027-03-18"),   # 次年重述
    ]
    d = cs._duration_facts(_facts(rows), CONCEPT)
    rec = d[("2025-11-28", "2026-02-26")]
    assert rec["filed"] == "2026-03-19" and round(rec["val"] / 1e6) == 6105


@pytest.mark.parametrize("days_span,keep", [
    (("2026-02-26", "2026-02-26"), False),   # 时点事实(0 天)
    (("2025-08-29", "2026-02-26"), True),    # 181 天 YTD
    (("2024-08-29", "2026-02-26"), False),   # 546 天,超出 380 上限
])
def test_only_quarter_to_annual_durations_are_kept(days_span, keep):
    s, e = days_span
    d = cs._duration_facts(_facts([(s, e, 100, 2026, "Q2", "2026-03-19")]), CONCEPT)
    assert (len(d) == 1) is keep


def test_missing_concept_returns_empty_not_raise():
    """概念不存在时返回空 —— 调用方靠 falsy 结果切换到下一个候选概念。"""
    assert cs._duration_facts({"facts": {"us-gaap": {}}}, CONCEPT) == {}
    assert cs._standalone_quarters({"facts": {"us-gaap": {}}}, CONCEPT) == {}
