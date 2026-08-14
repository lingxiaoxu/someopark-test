"""Test 20(plan §六):Polygon 失败注入 —— stale 标记 / 不换源 / 恢复自愈。
外加:分钟聚合备通道(§三)与 splits 日历(§九-7)的解析/传导测试。
真实结构装配(只读持仓文件)+ FakeFeed 注入,零网络;输出全部进 /tmp。
运行: conda run -n someopark_run python -m controller.tests.test_stale_feed
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("POLYGON_API_KEY", "TEST-KEY-NO-NETWORK")

import controller.scheduler as sched
from controller.prices import PriceFeed
from controller.registry import Registry

N = 0
def ok(name, cond):
    global N
    assert cond, f"FAILED: {name}"
    N += 1
    print(f"ok - {name}")


class FakeFeed:
    """market open 常开;fail=True 时 snapshot 抛异常(模拟 Polygon 全挂)。"""
    def __init__(self, prices):
        self.prices = dict(prices)   # {isin: price}
        self.fail = False
        self.splits = {}

    def market_status(self):
        return {"market": "open", "server_time": None}

    def snapshot(self, isins):
        if self.fail:
            raise RuntimeError("injected polygon outage")
        return {"prices": {i: self.prices[i] for i in isins if i in self.prices},
                "ts": {}, "missing": [i for i in isins if i not in self.prices],
                "backfilled": [], "feed_delay_min": 0.5, "asof": "test"}

    def splits_today(self, isins):
        return dict(self.splits)


# ── 沙箱:输出只进 /tmp(repo 测试纪律)────────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="controller_test20_")
sched.OUT_DIR = tmp
print(f"[test] OUT_DIR sandboxed to {tmp}")

c = sched.Controller(feed=FakeFeed({}))
leaves = sorted(c.S.leaves())
feed = FakeFeed({leaf: 100.0 + k for k, leaf in enumerate(leaves)})
feed.splits = {}
c.feed = feed

# 1) 正常 tick 建立基线
out1 = c.tick()
ok("20a 正常 tick 发布", out1.get("portfolio") is not None and not out1["stale"])
v_before = out1["portfolio"]

# 2) 注入故障:stale 标记 + 沿用 last_price(值不动)+ 不换源(无新价来源)
feed.fail = True
out2 = c.tick()
ok("20b 故障 tick 标 stale", out2["stale"] is True)
ok("20c stale 沿用 last_price,组合值不变", out2["portfolio"] == v_before)
nav = json.load(open(os.path.join(tmp, "nav_latest.json")))
ok("20d nav_latest 落盘 stale=true", nav["stale"] is True)
stream = [f for f in os.listdir(tmp) if f.startswith("nav_stream_") and f.endswith(".csv")]
lines = open(os.path.join(tmp, stream[0])).read().strip().split("\n")
_hdr = lines[0].split(",")
ok("20e nav_stream 行打 stale 标记",
   lines[-1].split(",")[_hdr.index("stale")] == "1")
ok("20e2 nav_stream 行带 structure_hash(对账取当时快照用)",
   len(lines[-1].split(",")[_hdr.index("structure_hash")]) == 16)

# 3) 恢复自愈:新价进来,stale 消除、值更新
feed.fail = False
feed.prices = {k: v * 1.01 for k, v in feed.prices.items()}
out3 = c.tick()
ok("20f 恢复后 stale 消除", out3["stale"] is False)
ok("20g 恢复后值随新价更新", abs(out3["portfolio"] - v_before) > 1e-9)

# 4) splits 传导:当日 split 票 → 含它的节点行 corp_action=true
split_leaf = leaves[0]
feed.splits = {split_leaf: "2:1"}
c._splits_date = None                     # 强制重拉日历
out4 = c.tick()
nav = json.load(open(os.path.join(tmp, "nav_latest.json")))
holders = {n["node_id"] for n in nav["nodes"] if n["corp_action"]}
expect = {nid for nid in c.fe.order if split_leaf in c.fe.expanded.get(nid, {})}
ok("20h corp_action 恰好标注所有含 split 票的节点", holders == expect and holders)
ok("20i nav_latest 携带 corp_actions 字典", len(nav["corp_actions"]) == 1)
ok("20j 结构未变时 structure_diff 为空", nav["structure_diff"] == [])
ok("20k last_rebuild_ts 存在", bool(nav["last_rebuild_ts"]))

# 5) rebuild 失败不致死(盘中半更新窗:inventory 已写 account 未写 → 装配拒绝)
#    → 沿用旧结构继续发布 + rebuild_error 标记;文件一致后自动恢复
orig_assemble = sched.assemble
def _boom(reg=None):
    raise RuntimeError("injected half-updated positions")
sched.assemble = _boom
c.watch_digest = "force-rebuild-attempt"
out5 = c.tick()
ok("20p 装配失败沿用旧结构继续发布", out5.get("portfolio") is not None)
nav = json.load(open(os.path.join(tmp, "nav_latest.json")))
ok("20q nav_latest 带 rebuild_error", "injected" in (nav.get("rebuild_error") or ""))
sched.assemble = orig_assemble
c._failed_digest = None                   # 模拟文件 digest 变化(account 跟上)
c.watch_digest = "force-rebuild-attempt"
out6 = c.tick()
nav = json.load(open(os.path.join(tmp, "nav_latest.json")))
ok("20r 文件一致后自动恢复", nav.get("rebuild_error") is None)

# 6) 备通道解析(monkeypatch _get,零网络):snapshot 缺口 → aggs 补上
reg = Registry()
pf = PriceFeed.__new__(PriceFeed)         # 跳过 __init__ 的 key 检查
pf.reg, pf.key, pf.consecutive_failures = reg, "TEST", 0
isin0 = leaves[0]
tick0 = reg.master[isin0]["polygon_ticker"]
def fake_get(path, params=None, retries=3):
    if path.startswith("/v2/aggs/ticker/"):
        assert f"/{tick0}/" in path, "备通道必须还是 Polygon 同源"
        return {"results": [{"c": 55.5, "t": 1765000000000}]}
    if path.startswith("/v2/snapshot"):
        return {"tickers": []}            # 主通道空手而归
    if path.startswith("/v3/reference/splits"):
        return {"results": [{"ticker": tick0, "split_from": 3, "split_to": 1}]}
    raise AssertionError(f"unexpected path {path}")
pf._get = fake_get
snap = pf.snapshot([isin0])
ok("20l 主通道缺口由分钟聚合补上", snap["prices"][isin0] == 55.5)
ok("20m backfilled 如实上报", snap["backfilled"] == [isin0])
ok("20n 补上后不再 missing", snap["missing"] == [])
ok("20o splits_today 解析 ticker→ISIN", pf.splits_today([isin0]) == {isin0: "3:1"})

# 7) 常驻循环 fail-not-die(2026-08-14 回归:8/13 一次 DNS 失败杀死进程 22h)
#    7a) market_status 抛异常 → 沿用上轮 market,tick 照常出值
c.feed = feed
c.tick()                                   # 先建立 last_status(open)
class DNSDeadFeed(FakeFeed):
    def market_status(self):
        raise RuntimeError("Failed to resolve 'api.polygon.io' ([Errno 8])")
dead = DNSDeadFeed({leaf: 101.0 + k for k, leaf in enumerate(leaves)})
c.feed = dead
out7 = c.tick()
ok("20s market_status 失败不抛穿", "skipped" not in out7)
nav = json.load(open(os.path.join(tmp, "nav_latest.json")))
ok("20t 沿用上轮 market", nav["market"] == "open")
ok("20u degraded 如实上报", "Failed to resolve" in (nav.get("market_degraded") or ""))

#    7b) 整轮异常 → run() 记 tick_error 并继续下一轮,不终止进程
c.feed = feed
boom = {"n": 0}
def exploding_tick(force=False):
    boom["n"] += 1
    if boom["n"] <= 2:
        raise RuntimeError("injected total tick failure")
    return {"ts": "t", "portfolio": 1.0, "stale": False}
laps = {"n": 0}
def fake_sleep(_s):
    laps["n"] += 1
    if laps["n"] >= 4:                     # 跑满 4 轮后收工(不是异常退出)
        raise KeyboardInterrupt
orig_sleep, c.tick = sched.time.sleep, exploding_tick
c._maybe_rebuild = lambda: False
sched.time.sleep = fake_sleep
try:
    c.run(interval_s=0, watch_s=0)
except KeyboardInterrupt:
    pass
finally:
    sched.time.sleep = orig_sleep
ok("20v 单轮异常不终止循环", laps["n"] >= 4 and boom["n"] >= 3)
ok("20w 失败后自愈清空 tick_error", c.tick_error is None)
ok("20x 连败计数在自愈后归零", c.tick_fail_streak == 0)

#    7c) registry 磁盘补录要能被常驻进程看见(8/14 JNJ 卡住的根因:
#        Registry 只在 __init__ 加载一次,`--build-master` 补完新票也不生效)
from controller.model import WATCH_FILES
before = c.reg
c._rebuild(replay=False)
ok("20y _rebuild 重读 registry(非同一实例)", c.reg is not before)
ok("20z security_master 进 WATCH_FILES",
   any("security_master" in f for f in WATCH_FILES))

print(f"\nall {N} checks passed")
