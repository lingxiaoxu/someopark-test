"""plan_orders 纯函数(深检修复 2026-08-17;兑现 M2 diff 单测承诺)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lean"))
from mirror_logic import plan_orders  # noqa: E402


def test_basic_build_and_converge():
    orders, conv = plan_orders({"A": 10, "B": -5}, {}, set())
    assert sorted(orders) == [("A", 10, "adjust"), ("B", -5, "adjust")]
    assert conv is False                     # 出单轮必未收敛(等成交)
    orders2, conv2 = plan_orders({"A": 10, "B": -5}, {"A": 10, "B": -5}, set())
    assert orders2 == [] and conv2 is True   # 全部到位 → 收敛,才可标版本


def test_blocked_skipped_and_not_converged():
    """新订阅无价/在途单: 跳过不出单、绝不收敛 —— 版本不标记,下一轮重试。
    (旧实现的 go-live 翻车根因: 提交即标版本,被拒腿永久丢失。)"""
    orders, conv = plan_orders({"A": 10, "B": 3}, {"A": 0, "B": 0}, {"A"})
    assert orders == [("B", 3, "adjust")]
    assert conv is False
    # A 下一轮解禁后补上
    orders2, conv2 = plan_orders({"A": 10, "B": 3}, {"A": 0, "B": 3}, set())
    assert orders2 == [("A", 10, "adjust")] and conv2 is False


def test_flatten_non_target():
    orders, conv = plan_orders({"A": 5}, {"A": 5, "Z": 7, "Y": -4}, set())
    assert ("Z", -7, "flatten") in orders and ("Y", 4, "flatten") in orders
    assert conv is False


def test_flatten_blocked_defers():
    orders, conv = plan_orders({}, {"Z": 7}, {"Z"})
    assert orders == [] and conv is False


def test_partial_fill_reconverges():
    """部分成交(delta 缩小)→ 只补差额,幂等。"""
    orders, _ = plan_orders({"A": 100}, {"A": 60}, set())
    assert orders == [("A", 40, "adjust")]


def test_short_targets():
    orders, _ = plan_orders({"S": -1518}, {"S": 0}, set())
    assert orders == [("S", -1518, "adjust")]
