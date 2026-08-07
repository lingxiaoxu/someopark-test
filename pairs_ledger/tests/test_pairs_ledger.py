"""pairs_ledger 单元测试（someopark_run）。

    conda run -n someopark_run python -m pytest pairs_ledger/tests -q

覆盖：拆股/**合股**归一 + 留痕守卫 + 多空记账 + 恒等式 + 与 portfolio_ledger 对拍。
"""
from __future__ import annotations

import pandas as pd
import pytest

from pairs_ledger.ledger import Account, INITIAL_CASH
from pairs_ledger.rebuild import flatten, normalize_snapshots


def _sp(ticker, ed, frm, to, sid):
    return {"ticker": ticker, "execution_date": ed, "split_from": frm,
            "split_to": to, "id": sid}


def _snap(shares1, price1, applied=None, open_date="2026-05-20"):
    # open_date 是真实快照必有的字段，且是 CorporateActions.adjust_position_view
    # 的守卫 2（拆股后开的仓天然新口径）—— 测试必须带上，否则测的不是生产行为。
    p = {"direction": "long", "open_date": open_date,
         "s1_shares": shares1, "open_s1_price": price1,
         "s2_shares": -100, "open_s2_price": 50.0}
    if applied:
        p["applied_corporate_actions"] = applied
    return {"2026-06-01": {"AAA/BBB": p}}


# ── 拆股 / 合股 归一 ────────────────────────────────────────────────────────

def test_forward_split_normalizes_shares_up_and_price_down():
    """正拆 1:10 → 股数 ×10、开仓价 ÷10，美元敞口不变。"""
    snaps = _snap(67, 2011.39)
    splits = {"AAA": [_sp("AAA", "2026-06-12", 1, 10, "id1")]}
    out, log = normalize_snapshots(snaps, splits)
    leg = out["2026-06-01"]["AAA/BBB"]
    assert leg["s1_shares"] == 670
    assert leg["open_s1_price"] == pytest.approx(201.139)
    # 美元敞口守恒（这是归一的全部意义）
    assert 67 * 2011.39 == pytest.approx(670 * 201.139)
    assert log and log[0][3] == pytest.approx(10.0)


def test_reverse_split_normalizes_shares_down_and_price_up():
    """**合股 10:1** → factor=0.1，股数 ×0.1、开仓价 ÷0.1(=×10)。

    同一式子 factor=to/from 两向都成立，无需分支 —— 这正是镜像
    portfolio_ledger 的原因（自己发明的经验判定处理不了合股）。
    """
    snaps = _snap(6700, 20.11)
    splits = {"AAA": [_sp("AAA", "2026-06-12", 10, 1, "id2")]}
    out, _ = normalize_snapshots(snaps, splits)
    leg = out["2026-06-01"]["AAA/BBB"]
    assert leg["s1_shares"] == 670
    assert leg["open_s1_price"] == pytest.approx(201.1)
    assert 6700 * 20.11 == pytest.approx(670 * 201.1)


def test_position_opened_after_split_is_not_renormalized():
    """守卫 2：拆股后开的仓天然是新口径，不得再归一。"""
    snaps = _snap(670, 201.14, open_date="2026-05-20")      # 开仓晚于 execution
    splits = {"AAA": [_sp("AAA", "2026-05-01", 1, 10, "id3")]}
    out, log = normalize_snapshots(snaps, splits)
    assert out["2026-06-01"]["AAA/BBB"]["s1_shares"] == 670
    assert not log


def test_applied_corporate_actions_guard_prevents_double_normalization():
    """留痕守卫：CorporateActions 在拆股日**盘前**改写 inventory，
    故 as_of=6/11 的快照已是新口径（早于 execution_date=6/12）。
    只按日期判断会二次归一 —— 实测这会把 MTFS 净值推到 +139 万。"""
    applied = [{"ticker": "AAA", "execution_date": "2026-06-12",
                "factor": 10.0, "polygon_id": "id4"}]
    snaps = _snap(670, 201.14, applied=applied)
    splits = {"AAA": [_sp("AAA", "2026-06-12", 1, 10, "id4")]}
    out, log = normalize_snapshots(snaps, splits)
    assert out["2026-06-01"]["AAA/BBB"]["s1_shares"] == 670, "不得二次归一"
    assert not log


def test_multiple_splits_compound():
    """多次拆/合按 execution_date 连乘。"""
    snaps = _snap(10, 1000.0)
    splits = {"AAA": [_sp("AAA", "2026-06-12", 1, 2, "a"),
                      _sp("AAA", "2026-07-01", 1, 5, "b")]}
    out, _ = normalize_snapshots(snaps, splits)
    leg = out["2026-06-01"]["AAA/BBB"]
    assert leg["s1_shares"] == 100                      # ×2 ×5
    assert leg["open_s1_price"] == pytest.approx(100.0)


# ── 展平 ────────────────────────────────────────────────────────────────────

def test_flatten_nets_same_ticker_across_pairs():
    """同一票在多个 pair → 记净敞口，否则会造出互相抵消的幽灵成交。"""
    pairs = {
        "AVB/X": {"direction": "short", "s1_shares": -722, "open_s1_price": 169.14,
                  "s2_shares": 10, "open_s2_price": 5.0},
        "AVB/Y": {"direction": "short", "s1_shares": -644, "open_s1_price": 166.11,
                  "s2_shares": 20, "open_s2_price": 6.0},
    }
    net, _ = flatten(pairs)
    assert net["AVB"] == -1366


def test_flatten_skips_closed_pairs():
    assert flatten({"A/B": {"direction": None, "s1_shares": 5}})[0] == {}


# ── 多空记账 ────────────────────────────────────────────────────────────────

def _acct():
    return Account.open_flat("mrpt", "2026-03-19", root="/tmp")


def test_short_open_increases_cash_and_realizes_correctly():
    """开空: cash 增加; 价格下跌后平仓 = 盈利。"""
    a = _acct()
    a.trade("2026-03-19", "S", -100, 50.0)
    assert a.data["cash"] == INITIAL_CASH + 5000
    assert a.data["positions"]["S"]["shares"] == -100
    r = a.trade("2026-03-20", "S", 100, 40.0)            # 低价买回
    assert r["realized_pnl"] == pytest.approx(1000.0)    # (50-40)*100
    assert "S" not in a.data["positions"]


def test_long_realized_matches_portfolio_ledger_semantics():
    a = _acct()
    a.trade("2026-03-19", "L", 100, 10.0)
    r = a.trade("2026-03-20", "L", -40, 12.0)
    assert r["realized_pnl"] == pytest.approx(80.0)      # (12-10)*40
    assert a.data["positions"]["L"]["avg_cost"] == pytest.approx(10.0)


def test_weighted_average_cost_on_add():
    a = _acct()
    a.trade("2026-03-19", "L", 100, 10.0)
    a.trade("2026-03-20", "L", 100, 20.0)
    assert a.data["positions"]["L"]["avg_cost"] == pytest.approx(15.0)


def test_flip_realizes_then_reopens_at_new_price():
    """翻向：先平尽实现盈亏，余量按新方向以成交价起算成本。"""
    a = _acct()
    a.trade("2026-03-19", "F", 100, 10.0)
    r = a.trade("2026-03-20", "F", -150, 12.0)
    assert r["realized_pnl"] == pytest.approx(200.0)     # (12-10)*100
    assert a.data["positions"]["F"]["shares"] == -50
    assert a.data["positions"]["F"]["avg_cost"] == pytest.approx(12.0)


def test_short_pays_dividend():
    """空头付股息（融券方需补付）—— 总额为负。"""
    a = _acct()
    a.trade("2026-03-19", "S", -100, 50.0)
    row = a.dividend("2026-03-20", "S", 0.5)
    assert row["gross"] == pytest.approx(-50.0)
    assert a.data["cumulative_dividends"] == pytest.approx(-50.0)


# ── 恒等式 ──────────────────────────────────────────────────────────────────

def test_identity_holds_for_mixed_long_short():
    """equity − 1M == realized + div − fees + unrealized（mark 内断言）。"""
    a = _acct()
    a.trade("2026-03-19", "L", 100, 10.0)
    a.trade("2026-03-19", "S", -200, 20.0)
    a.dividend("2026-03-19", "L", 0.25)
    a.fee("2026-03-19", 12.5)
    eq = a.mark("2026-03-19", pd.Series({"L": 11.0, "S": 19.0}))
    lhs = round(eq - INITIAL_CASH, 2)
    rhs = round(a.data["cumulative_realized"] + a.data["cumulative_dividends"]
                - a.data["cumulative_fees"] + a.data["unrealized"], 2)
    assert lhs == pytest.approx(rhs, abs=0.05)


def test_mark_refuses_missing_price():
    """缺价拒绝 mark —— 绝不用陈旧价冒充当日。"""
    a = _acct()
    a.trade("2026-03-19", "L", 100, 10.0)
    with pytest.raises(AssertionError, match="缺"):
        a.mark("2026-03-19", pd.Series({"OTHER": 1.0}))


def test_identity_breach_raises():
    a = _acct()
    a.trade("2026-03-19", "L", 100, 10.0)
    a.data["cumulative_realized"] += 999          # 人为破坏
    with pytest.raises(AssertionError, match="恒等式破裂"):
        a.mark("2026-03-19", pd.Series({"L": 10.0}))


def test_missing_open_date_falls_back_to_as_of():
    """open_date 缺失时用 as_of 代入（保守：只会少归一，不会误归一）。"""
    snaps = {"2026-06-01": {"AAA/BBB": {"direction": "long", "s1_shares": 670,
                                        "open_s1_price": 201.14, "s2_shares": -100,
                                        "open_s2_price": 50.0}}}
    splits = {"AAA": [_sp("AAA", "2026-05-01", 1, 10, "idX")]}   # execution < as_of
    out, log_ = normalize_snapshots(snaps, splits)
    assert out["2026-06-01"]["AAA/BBB"]["s1_shares"] == 670
    assert not log_


def test_delegates_to_corporate_actions():
    """归一必须走 CorporateActions —— 不允许 pairs_ledger 自带第二套拆股逻辑。"""
    import inspect

    from pairs_ledger import rebuild as R
    src = inspect.getsource(R.normalize_snapshots)
    body = src.replace(R.normalize_snapshots.__doc__ or "", "")   # 去掉文档字符串
    assert "adjust_position_view" in body, "必须委托 CorporateActions"
    assert "split_to" not in body, "不得在此重新实现 factor 计算（应只有一处真源）"
