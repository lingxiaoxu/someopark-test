"""ASML 前瞻需求换源(industry_signals._parse_guidance / supply_chain._asml_tilt,2026-08-27)。

**为什么必须有这个测试文件**: ASML 从 2026Q1 起把季度 net bookings **整行**从财报里
删了 —— 不是措辞变了(2026-04-15 / 2026-07-15 全篇零命中),没有任何正则能捞回一个
没发布的数字。equipment 那条外部确认 tilt 因此在 2026-01-28 之后一直吃冻结值,而
`if len(x)` 式的旧体检照打 OK。

换上来的是**下季度净销售指引**(每季必发、同为前瞻量)。两个坑各钉一半:

1. 解析侧: 交易所 HTML 会把一个单词拆进相邻的内联标签,按 ``<[^>]+>`` → " " 清洗
   会得到 "total net sale s between"(2024-07-17 就是这样),按词写的正则必漏。
2. 拼接侧: bookings(新增订单额)与 guidance(预期营收)口径不同,先拼后 z 会把
   口径切换本身当成一次几个标准差的跳变。必须**各自** z 再按优先级取。

沙箱纪律: 全部用手写 HTML / 合成序列,不联网、不读写任何生产文件。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[3]              # someopark-test/
for p in (str(_ROOT), str(_ROOT / "qlib-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

from semiconductor_strategy.data import industry_signals as ind   # noqa: E402
from semiconductor_strategy.signals import supply_chain as sc     # noqa: E402


# ── 解析: 被标签劈开的单词 ──────────────────────────────────────────────────

def test_squash_reglues_a_word_split_across_tags():
    """去标签时**不能**插空格,否则 `<i>sale</i><i>s</i>` 变成 "sale s"。"""
    assert "netsales" in ind._squashed_text("<span>net sale</span><span>s</span>")


def test_nbsp_and_euro_sign_are_normalised():
    """&nbsp;(unescape 成 \\xa0)与 € 都要落到与正则一致的形态上。"""
    t = ind._squashed_text("EUR&nbsp;7.1 &euro;9.0")
    assert t == "eur7.1eur9.0"


# 2024-07-17 的真实形状: "sales" 被拆开、数字带内联标签、单位只在第二个数上出现。
_SPLIT_HTML = (
    "<p>ASML <b>expects Q3 2024 total net sale</b><b>s</b> between "
    "<span>&euro;6.7</span> billion and <span>&euro;7.3</span> billion.</p>"
)
# "of between" 变体(2023-10-18 一类的写法)。
_OF_BETWEEN_HTML = (
    "<p>ASML expects Q4 2023 total net sales of between &euro;6.7 billion "
    "and &euro;7.1 billion.</p>"
)


@pytest.mark.parametrize("html,quarter,lo,hi,mid", [
    (_SPLIT_HTML,      "2024Q3", 6.7, 7.3, 7.0),
    (_OF_BETWEEN_HTML, "2023Q4", 6.7, 7.1, 6.9),
])
def test_parses_both_real_sentence_shapes(html, quarter, lo, hi, mid):
    got = ind._parse_guidance(html)
    assert got == {"quarter": quarter, "low_eur_bn": lo,
                   "high_eur_bn": hi, "mid_eur_bn": mid}


def test_quarter_key_is_the_guided_quarter_not_the_reporting_one():
    """记录键必须是被指引的**未来**季度。

    用发布季当键会让两条相邻 6-K 互相覆盖,序列直接少一半。
    """
    assert ind._parse_guidance(_SPLIT_HTML)["quarter"] == "2024Q3"   # 发布于 2024-07(Q2 财报)


# ── 解析: 不该命中的都不能命中 ──────────────────────────────────────────────

@pytest.mark.parametrize("html", [
    # ASML 没附新闻稿时那些 exhibit 里的定性表述(11 份 6-K 真实内容)
    "<p>we expect continued strong growth with a net sales increase towards 30%</p>",
    "<p>expected financial results, including expected net sales, gross margin, R&amp;D cost</p>",
    "<p>order intake remained extremely strong</p>",
    "",
])
def test_qualitative_text_yields_nothing(html):
    """宁可返回 None 让体检报 STALE,也不能瞎猜一个数写进存储。"""
    assert ind._parse_guidance(html) is None


@pytest.mark.parametrize("lo,hi", [
    (7.3, 6.7),        # 区间反了
    (0.1, 0.2),        # 量级离谱(百万当十亿读)
    (150.0, 200.0),    # 量级离谱(另一头)
])
def test_insane_ranges_are_rejected_not_stored(lo, hi):
    html = (f"<p>ASML expects Q3 2024 total net sales between &euro;{lo} billion "
            f"and &euro;{hi} billion.</p>")
    assert ind._parse_guidance(html) is None


# ── 拼接: z 空间,不是水平空间 ──────────────────────────────────────────────

_MIDX = pd.date_range("2019-01-31", periods=72, freq="ME")


def _daily(values_by_month: pd.Series) -> pd.Series:
    """把月度值摊成日频 ffill 序列(模拟 pit.reindex_pit_daily 的产物)。"""
    idx = pd.date_range(values_by_month.index[0], _MIDX[-1], freq="D")
    return values_by_month.reindex(idx, method="ffill").ffill()


def _wave(index, base, amp, phase=0.0):
    return pd.Series(base + amp * np.sin(np.arange(len(index)) / 6.0 + phase), index=index)


ORDERS = _daily(_wave(_MIDX, base=6.0, amp=1.5))                 # 全程可得
GUID = _daily(_wave(_MIDX[36:], base=8.0, amp=2.0, phase=1.0))   # 第 37 个月才开始


def test_history_before_guidance_matures_is_bit_identical():
    """指引攒够 z 窗口之前必须逐位回落到 bookings —— 换源不得改写旧回测。"""
    before = sc._asml_tilt(ORDERS, None, _MIDX)
    after = sc._asml_tilt(ORDERS, GUID, _MIDX)
    n = 36 + sc._TS_Z_MIN - 1          # 指引第一个非 NaN 的 z 出现在第 36+12 个月
    pd.testing.assert_series_equal(after.iloc[:n], before.iloc[:n])
    assert not np.allclose(after.iloc[n:], before.iloc[n:]), "近端应当已经换成指引"


def test_guidance_wins_once_available():
    after = sc._asml_tilt(ORDERS, GUID, _MIDX)
    guid_only = sc._ts_zscore(sc._to_monthly(GUID, _MIDX))
    tail = guid_only.notna()
    pd.testing.assert_series_equal(after[tail], guid_only[tail].fillna(0.0),
                                   check_names=False)


def test_unit_mismatch_cannot_leak_into_the_signal():
    """指引整体放大 10 倍(比如口径从 bn 变 mn),tilt 必须**一位不变**。

    这正是"先拼后 z"会砸掉的性质: 那样做的话切换点会凭空冒出一个几倍标准差的跳变,
    而它只反映记账单位,不反映任何需求信息。
    """
    a = sc._asml_tilt(ORDERS, GUID, _MIDX)
    b = sc._asml_tilt(ORDERS, GUID * 10.0, _MIDX)
    pd.testing.assert_series_equal(a, b)


def test_no_sources_means_no_tilt_not_zeros():
    """两条都没有 → None,让调用方彻底跳过 tilt(V1 纯价格代理行为)。

    返回全 0 序列看起来一样,但会把 `score["equipment"] + 0.30*0` 这一步做实,
    未来任何非零默认值都会静默进产。
    """
    assert sc._asml_tilt(None, None, _MIDX) is None
    assert sc._asml_tilt(pd.Series(dtype="float64"), None, _MIDX) is None


def test_guidance_alone_is_enough():
    """bookings 存储被清空/尚未初始化时,指引单独也要能撑起 tilt。"""
    out = sc._asml_tilt(None, GUID, _MIDX)
    assert out is not None and out.index.equals(_MIDX) and out.notna().all()
