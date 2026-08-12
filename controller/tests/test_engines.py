"""M2 引擎测试:PDF 14 用例移植 + plan 扩展(修 A/cash/热更新)。
每条用例两引擎并跑,断言 emissions 与全层级 values 一致(整数价精确相等)。
运行: conda run -n someopark_run python controller/tests/test_engines.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from controller.engine_flatten import FlattenEngine, CycleError
from controller.engine_tree import TreeEngine

N = 0
def ok(name, cond):
    global N
    assert cond, f"FAILED: {name}"
    N += 1
    print(f"ok - {name}")


class FakeStructure:
    """与 model.Structure 同形的测试结构。"""
    def __init__(self, defs, cash=None):
        # defs: {name: [(child, shares), ...]};cash: {name: cash_const}
        self.nodes = {n: {"id": n, "kind": "portfolio",
                          "children": list(ch),
                          "attrs": ({"cash_const": (cash or {}).get(n, 0.0)})}
                      for n, ch in defs.items()}


def run_both(defs, ticks, cash=None):
    """两引擎并跑:返回 (每tick的emits序列, 终态values)——先断言两引擎一致。"""
    fs = FakeStructure(defs, cash)
    fe, te = FlattenEngine(fs), TreeEngine(fs)
    f_stream = [fe.initial_emissions()]
    t_stream = [te.initial_emissions()]
    for tick in ticks:
        f_stream.append(fe.apply_tick(dict(tick)))
        t_stream.append(te.apply_tick(dict(tick)))
    assert f_stream == t_stream, \
        f"engines disagree:\nflatten={f_stream}\ntree   ={t_stream}"
    assert fe.values() == te.values(), \
        f"values disagree:\nflatten={fe.values()}\ntree={te.values()}"
    return f_stream, fe.values()


# ── 1. PDF 精确例(brief example)────────────────────────────────────────────
defs = {"TECH": [("AAPL", 100), ("MSFT", 200), ("NVDA", 300)]}
stream, vals = run_both(defs, [[("AAPL", 173), ("MSFT", 425), ("NVDA", 880)],
                               [("AAPL", 174)]])
ok("1 brief: TECH=366300 after tick1", stream[1] == [("TECH", 366300.0)])
ok("1 brief: TECH=366400 after AAPL 174", stream[2] == [("TECH", 366400.0)])

# ── 2. 嵌套 + gating ─────────────────────────────────────────────────────────
defs = {"TECH": [("AAPL", 100), ("MSFT", 200)], "AUTOS": [("TSLA", 10), ("FORD", 20)],
        "INDUSTRIALS": [("TECH", 2), ("AUTOS", 3)]}
stream, vals = run_both(defs, [[("AAPL", 10), ("MSFT", 20)],
                               [("TSLA", 30), ("FORD", 40)]])
ok("2 gating: tick1 只发 TECH", stream[1] == [("TECH", 5000.0)])
ok("2 gating: tick2 发 AUTOS+INDUSTRIALS(子先父后)",
   stream[2] == [("AUTOS", 1100.0), ("INDUSTRIALS", 13300.0)])

# ── 3. 重定价只发变化的组合 ──────────────────────────────────────────────────
stream, _ = run_both(defs, [[("AAPL", 10), ("MSFT", 20)], [("TSLA", 30), ("FORD", 40)],
                            [("AAPL", 11)]])
ok("3 reprice: AAPL 变只发 TECH+INDUSTRIALS",
   stream[3] == [("TECH", 5100.0), ("INDUSTRIALS", 13500.0)])

# ── 4. 无关股票不扰动 ────────────────────────────────────────────────────────
defs4 = {"TECH": [("AAPL", 100)]}
stream, _ = run_both(defs4, [[("GOLD", 1500)], [("AAPL", 10)]])
ok("4 unrelated: GOLD 无 emit", stream[1] == [])
ok("4 unrelated: AAPL 正常", stream[2] == [("TECH", 1000.0)])

# ── 5. 深链先发最深 ─────────────────────────────────────────────────────────
defs5 = {"L4": [("STK", 2)], "L3": [("L4", 2)], "L2": [("L3", 2)], "L1": [("L2", 2)]}
stream, _ = run_both(defs5, [[("STK", 5)], [("STK", 10)]])
ok("5 deep chain: 顺序 L4→L1",
   stream[1] == [("L4", 10.0), ("L3", 20.0), ("L2", 40.0), ("L1", 80.0)])
ok("5 deep chain: 重定价同序",
   stream[2] == [("L4", 20.0), ("L3", 40.0), ("L2", 80.0), ("L1", 160.0)])

# ── 6. diamond(共享子组合,只发一次,两路都加)──────────────────────────────
defs6 = {"C": [("STK", 10)], "A": [("C", 1)], "B": [("C", 1)],
         "FUND": [("A", 1), ("B", 1)]}
stream, _ = run_both(defs6, [[("STK", 5)]])
ok("6 diamond: FUND=100 只出现一次",
   stream[1] == [("C", 50.0), ("A", 50.0), ("B", 50.0), ("FUND", 100.0)])

# ── 7. 直接持有 + 经子组合持有(shares 相加)─────────────────────────────────
defs7 = {"BANKS": [("JPM", 200)], "VALUE": [("BANKS", 1), ("JPM", 100)]}
stream, _ = run_both(defs7, [[("JPM", 2)]])
ok("7 direct+via-sub: VALUE=600 (JPM eff 300)",
   stream[1] == [("BANKS", 400.0), ("VALUE", 600.0)])

# ── 8. 负 shares 多空(pair 空腿)────────────────────────────────────────────
defs8 = {"LONG": [("AAPL", 100), ("MSFT", 50)], "SHORT": [("AAPL", -60)],
         "BOOK": [("LONG", 1), ("SHORT", 1)]}
stream, vals = run_both(defs8, [[("AAPL", 200), ("MSFT", 10)], [("AAPL", 205)]])
ok("8 long/short: BOOK=8500 (净 40 AAPL+50 MSFT)", vals["BOOK"] == 21000.0 - 12300.0)
ok("8 long/short: tick1 emits",
   stream[1] == [("LONG", 20500.0), ("SHORT", -12000.0), ("BOOK", 8500.0)])

# ── 9. 小数 shares ──────────────────────────────────────────────────────────
defs9 = {"P": [("AAPL", 1.5), ("MSFT", 0.25)]}
_, vals = run_both(defs9, [[("AAPL", 10), ("MSFT", 8)]])
ok("9 fractional: P=17.0", vals["P"] == 17.0)

# ── 10. 全对冲名字动价不触发重估 ─────────────────────────────────────────────
defs10 = {"LONG": [("AAPL", 100)], "SHORT": [("AAPL", -100)], "MSFT_P": [("MSFT", 50)],
          "BOOK": [("LONG", 1), ("SHORT", 1), ("MSFT_P", 1)]}
stream, vals = run_both(defs10, [[("AAPL", 200), ("MSFT", 10)], [("AAPL", 205)]])
ok("10 hedged: BOOK=500 (AAPL 净 0)", vals["BOOK"] == 500.0)
f = FlattenEngine(FakeStructure(defs10))
ok("10 hedged: 拍平后 BOOK 不含 AAPL(净 0 删除)",
   "AAPL" not in f.expanded["BOOK"])
ok("10 hedged: AAPL 再动 BOOK 不重发(flatten 语义)",
   all(n != "BOOK" for n, _ in stream[2]))

# ── 11. 同价重复不重发 ───────────────────────────────────────────────────────
defs11 = {"P": [("AAPL", 100)]}
stream, _ = run_both(defs11, [[("AAPL", 10)], [("AAPL", 10)], [("AAPL", 11)]])
ok("11 repeated price: 第二次同价无 emit", stream[2] == [])
ok("11 repeated price: 变价才发", stream[3] == [("P", 1100.0)])

# ── 12. 定义顺序无关 ─────────────────────────────────────────────────────────
defs12a = {"INDUSTRIALS": [("TECH", 2)], "TECH": [("AAPL", 100), ("MSFT", 200)]}
defs12b = {"TECH": [("AAPL", 100), ("MSFT", 200)], "INDUSTRIALS": [("TECH", 2)]}
_, va = run_both(defs12a, [[("AAPL", 10), ("MSFT", 20)]])
_, vb = run_both(defs12b, [[("AAPL", 10), ("MSFT", 20)]])
ok("12 order independent", va == vb == {"TECH": 5000.0, "INDUSTRIALS": 10000.0})

# ── 13. 扇出(一股票在多组合)────────────────────────────────────────────────
defs13 = {f"P{i}": [("AAPL", i)] for i in range(1, 6)}
stream, _ = run_both(defs13, [[("AAPL", 10)]])
ok("13 fan-out: 5 组合全发",
   stream[1] == [(f"P{i}", 10.0 * i) for i in range(1, 6)])

# ── 14. 循环检测(报错停下)──────────────────────────────────────────────────
defs14 = {"A": [("B", 1)], "B": [("A", 1)]}
for Eng in (FlattenEngine, TreeEngine):
    try:
        Eng(FakeStructure(defs14))
        ok(f"14 cycle caught ({Eng.name})", False)
    except CycleError:
        ok(f"14 cycle caught ({Eng.__name__})", True)

# ── 15. 空策略(修 A:required==0 初始化发出,不 gating 父)────────────────────
defs15 = {"MRPT": [], "MTFS": [("AAPL", 100)],
          "PORTFOLIO": [("MRPT", 1), ("MTFS", 1)]}
cash15 = {"MRPT": 1_042_112.28, "MTFS": 648_151.70}
stream, vals = run_both(defs15, [[("AAPL", 10)]], cash=cash15)
ok("15 empty strategy: 初始化发出 MRPT=cash",
   ("MRPT", 1_042_112.28) in stream[0])
ok("15 empty strategy: PORTFOLIO 不被 gating,tick1 集齐",
   vals["PORTFOLIO"] == 1_042_112.28 + 648_151.70 + 1000.0)

# ── 16. 结构热更新 = 重建后与冷启动一致(修 C 语义)──────────────────────────
defs16a = {"S": [("AAPL", 100)]}
defs16b = {"S": [("AAPL", 100), ("MSFT", 50)]}          # 热更新:加一腿
fe = FlattenEngine(FakeStructure(defs16a)); fe.initial_emissions()
fe.apply_tick({"AAPL": 10})
lastp = dict(fe.last_price)
# 重建(修 C):新结构 + 重放 last_price
fe2, te2 = FlattenEngine(FakeStructure(defs16b)), TreeEngine(FakeStructure(defs16b))
fe2.initial_emissions(); te2.initial_emissions()
fe2.apply_tick(lastp); te2.apply_tick(lastp)
ok("16 hot-rebuild: 新叶未定价 → S gating(不发部分值)",
   fe2.values() == te2.values() == {})
fe2.apply_tick({"MSFT": 8}); te2.apply_tick({"MSFT": 8})
ok("16 hot-rebuild: 新叶到价后集齐一致",
   fe2.values() == te2.values() == {"S": 1400.0})

# ── 17. cash 拍平链(cash 不进 seen/required,但沿链聚合)─────────────────────
defs17 = {"SUB": [("AAPL", 10)], "TOP": [("SUB", 2)]}
cash17 = {"SUB": 100.0, "TOP": 7.0}
stream, vals = run_both(defs17, [[("AAPL", 1)]], cash=cash17)
ok("17 cash chain: TOP = 2×(10+100) + 7 = 227", vals["TOP"] == 227.0)
ok("17 cash chain: required 不含 cash",
   FlattenEngine(FakeStructure(defs17, cash17)).required["TOP"] == 1)

# ── 19. 规模/速度(PDF 基准:5000 股组合 5 万次更新)──────────────────────────
big = {"BIG": [(f"S{i}", 1) for i in range(5000)]}
t0 = time.time()
fs_big = FakeStructure(big)
fe_big, te_big = FlattenEngine(fs_big), TreeEngine(fs_big)
fe_big.initial_emissions(); te_big.initial_emissions()
for eng in (fe_big, te_big):
    eng.apply_tick({f"S{i}": 1.0 for i in range(5000)})   # 首轮全定价
    for j in range(50000):
        eng.apply_tick({f"S{j % 5000}": 2.0})
dt = time.time() - t0
ok(f"19 speed: 两引擎 5000×50k 共 {dt:.2f}s (<10s)", dt < 10)
ok("19 speed: 终态一致", fe_big.values() == te_big.values())

print(f"\nall {N} checks passed")
