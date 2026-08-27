"""PIT 数据时效检测器(aiss_pit.staleness / stale_tag,2026-08-27 新增)。

**为什么必须有这个测试文件**: 被它取代的旧 `verify()` 只查 `if len(x)` —— 有数据就
打 `RESULT: OK`。capex_pulse 从 2026-06-04 冻到 08-27(57 个交易日)期间,每周六的
weekly 体检都在日志里原样打印 `→2026-06-04` 然后 `RESULT: OK`,一次都没报警。
同期这条序列 + dram/tsmc/asml/mu_dio/pmi 一起喂 composite 的 0.70 权重,7/31 月末
真实 capex z 已翻负而生产仍用冻结的 +0.43,8/3 那次月度调仓方向做反。

检测器只在生产上"看着像对"是不够的 —— 那正是它上一次失效的方式。

沙箱纪律: 全部用注入的 as_of + 字面日期,不读任何生产文件、不落盘。
"""
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[3]              # someopark-test/
for p in (str(_ROOT), str(_ROOT / "qlib-main")):
    if p not in sys.path:
        sys.path.insert(0, p)

from semiconductor_strategy.data import aiss_pit as pit  # noqa: E402

AS_OF = "2026-08-27"                  # 周四,NYSE 开市


# ── 事故回放 ────────────────────────────────────────────────────────────────

def test_the_incident_would_have_been_caught():
    """capex_pulse 冻在 2026-06-04 → 必须 STALE。这条一红,事故就不会发生。"""
    age, unit, is_stale, limit = pit.staleness("2026-06-04", "daily", as_of=AS_OF)
    assert is_stale, "冻死 57 个交易日的日频序列必须被判为过期"
    assert age > 50 and unit == "交易日"
    assert limit == pit.STALE_TRADING_DAYS


def test_fresh_daily_series_is_clean():
    """修好之后(最后一点 = 前一交易日)不得报警,否则天天喊狼没人再看。"""
    _, _, is_stale, _ = pit.staleness("2026-08-26", "daily", as_of=AS_OF)
    assert not is_stale


# ── 阈值边界 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("last,expect_stale", [
    ("2026-08-20", False),   # 恰好 5 个交易日 = 上限,不报
    ("2026-08-19", True),    # 6 个交易日,越界
])
def test_daily_boundary_is_exclusive(last, expect_stale):
    assert pit.staleness(last, "daily", as_of=AS_OF)[2] is expect_stale


def test_weekend_and_holiday_do_not_trigger():
    """日频容忍度按**交易日**算的理由: 周末本就没有新点。

    2026-08-27(周四)回看上周五 2026-08-21 只隔 3 个交易日,却隔了 6 个自然日 ——
    若按自然日 + 5 天阈值就会误报。误报和漏报一样致命: 它教会人忽略这条告警。
    """
    age, unit, is_stale, _ = pit.staleness("2026-08-21", "daily", as_of=AS_OF)
    assert unit == "交易日" and age <= 4 and not is_stale


@pytest.mark.parametrize("cadence,limit_attr", [
    ("monthly", "STALE_MONTHLY_DAYS"),
    ("quarterly", "STALE_QUARTERLY_DAYS"),
])
def test_slow_cadence_uses_calendar_days(cadence, limit_attr):
    limit = getattr(pit, limit_attr)
    assert pit.staleness(AS_OF, cadence, as_of=AS_OF)[1] == "天"
    # 恰好压在上限上 → 不报;再早一天 → 报
    import pandas as pd
    on_limit = (pd.Timestamp(AS_OF) - pd.Timedelta(days=limit)).date().isoformat()
    over = (pd.Timestamp(AS_OF) - pd.Timedelta(days=limit + 1)).date().isoformat()
    assert pit.staleness(on_limit, cadence, as_of=AS_OF)[2] is False
    assert pit.staleness(over, cadence, as_of=AS_OF)[2] is True


# ── 真实序列的当前状态(把已知结论钉住)──────────────────────────────────────

def test_retired_and_repaired_feeds_after_the_2026_08_27_fix():
    """两条季频源当天各自的归宿 —— 一条换源、一条修好,都不再是"长期红着的告警"。

    asml_orders: ASML 从 2026Q1 起把季度 net bookings **整行删了**(2026-04-15 /
      2026-07-15 全篇零命中),最后一期永远停在 2026-01-28。它按检测器仍是 STALE,
      但 verify() 已把它标成 RETIRED 且不计入 ok —— 对一条上游已消失的序列天天报警
      只会制造告警疲劳。活的接续序列是 asml_guidance(下季度净销售指引)。
    mu_dio: 原先冻在 2025-12-18 的根因**不是** MU 只报 YTD(它确实标了 90 天单季),
      而是 sec.concept_series 按 end 单键去重把单季事实挤掉了(见
      test_xbrl_decumulation.py)。修好后最后一期是 2026-05-28,不再过期。
    """
    assert pit.staleness("2026-01-28", "quarterly", as_of=AS_OF)[2], "asml_orders 已退役"
    assert not pit.staleness("2026-05-28", "quarterly", as_of=AS_OF)[2], "mu_dio 已修复"


def test_refreshed_slow_feeds_are_clean():
    """2026-08-27 手工补数之后这几条应转绿,证明阈值不是一刀切地全红。"""
    assert not pit.staleness("2026-08-17", "monthly", as_of=AS_OF)[2], "tsmc 2026-07"
    assert not pit.staleness("2026-08-20", "monthly", as_of=AS_OF)[2], "pmi 2026-07"
    assert not pit.staleness("2026-07-31", "quarterly", as_of=AS_OF)[2], "hyperscaler capex"


# ── stale_tag 的契约 ────────────────────────────────────────────────────────

def test_stale_tag_is_empty_when_fresh_and_loud_when_not():
    """verify() 直接拿 `if tag:` 当判据 —— 新鲜必须是**空串**,否则全部误判过期。"""
    assert pit.stale_tag("2026-08-26", "daily", as_of=AS_OF) == ""
    tag = pit.stale_tag("2026-06-04", "daily", as_of=AS_OF)
    assert tag.startswith("  ← STALE (") and "交易日" in tag


def test_period_end_vs_release_date_matters():
    """季频必须按 filed/release 日算,不能按 period_end。

    MU 的 2026Q1 period_end=2025-11-27 但 filed=2025-12-18。若拿 period_end 当基准,
    每条季频数据一出生就"晚"了 90 天,120 天的阈值只剩 30 天余量 —— 阈值等于作废。
    这里钉住两者的差异,防止以后有人"顺手"改成 period_end。
    """
    by_filed = pit.staleness("2025-12-18", "quarterly", as_of=AS_OF)[0]
    by_period_end = pit.staleness("2025-11-27", "quarterly", as_of=AS_OF)[0]
    assert by_period_end - by_filed == 21


def test_bad_cadence_raises_instead_of_silently_degrading():
    """未知 cadence 必须**抛**,不得兜底成某个默认阈值。

    兜底成最松的季频阈值 = 写错字符串的那条序列从此永不报警,而日志照打 OK ——
    正是本模块要根治的失效模式。宁可 --verify 当场炸,也不要一条哑掉的检测器。
    """
    with pytest.raises(ValueError, match="未知 cadence"):
        pit.staleness("2026-08-01", "weekly-ish", as_of=AS_OF)
