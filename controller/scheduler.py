"""
controller/scheduler.py — tick 循环 + 结构 watcher + 双引擎对拍 + 输出流(M4)。

循环(plan §四):
  watcher(独立节拍,7×24):WATCH_FILES 内容摘要变化 → 立即全量重建两引擎
    + last_price 合成重放(修 C)→ nav_latest 即刻反映新持仓(盘后也生效)。
  行情 tick(interval,仅开市):Polygon 批量快照 → 两引擎 apply → 容差对拍
    (发布=树引擎,verifier=拍平)→ 定期全量 rebaseline 审计 → 落盘。

输出(controller/output/,数据 gitignore、代码入库):
  nav_latest.json                 最新全层级值(前端轮询;原子写)
  nav_stream_{YYYYMMDD}.csv       ts,node_id,display_name,kind,value,stale 追加流
  risk_matrix_latest.json         拍平引擎反向索引
  structure_snapshot_{hash}.json  每次重建留痕
对拍:|Δ| ≤ max(1e-6, 1e-9·|v|) 视为相等;超差 abort 不发布(plan §4.2)。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

from controller.registry import Registry
from controller.model import assemble, WATCH_FILES, REPO
from controller.engine_flatten import FlattenEngine
from controller.engine_tree import TreeEngine
from controller.prices import PriceFeed, et_today

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "output")

ABS_TOL, REL_TOL = 1e-6, 1e-9
REBASELINE_EVERY_S = 1800


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w"), indent=1, default=str)
    os.replace(tmp, path)


def _watch_digest() -> str:
    h = hashlib.sha1()
    for rel in WATCH_FILES:
        p = os.path.join(REPO, rel)
        try:
            with open(p, "rb") as fh:
                h.update(hashlib.sha1(fh.read()).digest())
        except FileNotFoundError:
            h.update(b"MISSING:" + rel.encode())
    return h.hexdigest()[:16]


class EngineDisagreement(RuntimeError):
    pass


def _diff_structures(old_nodes: dict, new_nodes: dict, reg) -> list[str]:
    """相邻两份结构的人话摘要('MTFS 5→4' 式;plan §4.3 持仓变化提示)。"""
    out = []
    strategies = {nid for nid, n in {**old_nodes, **new_nodes}.items()
                  if n["kind"] == "strategy"}
    for sid in sorted(strategies):
        o, n = old_nodes.get(sid), new_nodes.get(sid)
        name = reg.render(sid)
        if o is None or n is None:
            out.append(f"{name} {'新增' if o is None else '移除'}")
            continue
        oc = {c: q for c, q in o["children"]}
        nc = {c: q for c, q in n["children"]}
        if len(oc) != len(nc):
            out.append(f"{name} {len(oc)}→{len(nc)}")
        added = [reg.render(c) for c in nc.keys() - oc.keys()]
        removed = [reg.render(c) for c in oc.keys() - nc.keys()]
        resized = sum(1 for c in oc.keys() & nc.keys() if oc[c] != nc[c])
        if added:
            out.append(f"{name} 新开 {', '.join(sorted(added))}")
        if removed:
            out.append(f"{name} 平掉 {', '.join(sorted(removed))}")
        if resized:
            out.append(f"{name} {resized} 个持仓 shares 变化")
    return out


class Controller:
    def __init__(self, feed: PriceFeed | None = None):
        self.reg = Registry()
        self.feed = feed if feed is not None else PriceFeed(self.reg)
        self.last_price: dict[str, float] = {}
        self.watch_digest: str | None = None
        self.last_rebaseline = 0.0
        self.last_rebuild_ts: str | None = None
        self.structure_diff: list[str] = []
        self._splits: dict[str, str] = {}      # {leaf_isin: "from:to"} 当日 split
        self._splits_date: str | None = None
        self._rebuild(replay=False)

    # ── 结构(重)建 + 合成重放(修 C 统一语义)────────────────────────────────
    def _rebuild(self, replay: bool = True) -> None:
        prev_nodes = getattr(self, "S", None) and self.S.nodes
        self.S = assemble(self.reg)
        self.structure_diff = (_diff_structures(prev_nodes, self.S.nodes, self.reg)
                               if prev_nodes else [])
        self.last_rebuild_ts = _now_iso()
        self.fe = FlattenEngine(self.S)
        self.te = TreeEngine(self.S)
        init_f, init_t = self.fe.initial_emissions(), self.te.initial_emissions()
        if init_f != init_t:
            raise EngineDisagreement(f"init emissions differ: {init_f} vs {init_t}")
        if replay and self.last_price:
            replayable = {k: v for k, v in self.last_price.items()
                          if k in self.S.leaves()}
            if replayable:
                self.fe.apply_tick(replayable)
                self.te.apply_tick(replayable)
                self._verify("rebuild-replay")
        self.watch_digest = _watch_digest()
        snap_path = os.path.join(OUT_DIR, f"structure_snapshot_{self.S.hash}.json")
        if not os.path.exists(snap_path):
            _atomic_json({"hash": self.S.hash, "ts": _now_iso(),
                          "nodes": self.S.nodes,
                          "sources": self.S.sources}, snap_path)
        print(f"[controller] structure {'re' if replay else ''}built "
              f"hash={self.S.hash} nodes={len(self.S.nodes)} "
              f"leaves={len(self.S.leaves())}")

    # ── 对拍 + rebaseline ─────────────────────────────────────────────────────
    def _verify(self, ctx: str) -> None:
        vf, vt = self.fe.values(), self.te.values()
        if set(vf) != set(vt):
            raise EngineDisagreement(f"[{ctx}] priced sets differ")
        for k in vf:
            tol = max(ABS_TOL, REL_TOL * abs(vt[k]))
            if abs(vf[k] - vt[k]) > tol:
                raise EngineDisagreement(
                    f"[{ctx}] {k}: flatten={vf[k]!r} tree={vt[k]!r}")

    def _rebaseline(self) -> None:
        """第三重审计:直接全量求和,超容差以其为准重置两引擎(plan §4.2-3)。"""
        for nid in self.fe.order:
            if nid not in self.fe.fully_priced:
                continue
            direct = self.fe.cash_flat[nid] + sum(
                eff * self.last_price[leaf]
                for leaf, eff in self.fe.expanded[nid].items()
                if leaf in self.last_price)
            for eng, store in ((self.fe, self.fe.value), (self.te, self.te.total)):
                if abs(store[nid] - direct) > max(ABS_TOL, REL_TOL * abs(direct)):
                    print(f"!!!! [controller WARN] rebaseline {nid}: "
                          f"{eng.name}={store[nid]} direct={direct} — reset")
                    store[nid] = direct
                    eng.value_last_emitted[nid] = direct
        self.last_rebaseline = time.time()

    # ── 单轮 tick ─────────────────────────────────────────────────────────────
    def tick(self, force: bool = False) -> dict:
        # 1) watcher 兜底检查(独立轮询之外,tick 前必查)
        rebuilt = False
        if _watch_digest() != self.watch_digest:
            self._rebuild(replay=True)
            rebuilt = True

        # 2) 市场状态
        status = self.feed.market_status()
        closed = status["market"] == "closed"

        # 2b) 当日 split 日历(每 ET 日一次;plan §九-7:只标注,不改 shares)
        today_et = et_today()
        if self._splits_date != today_et:
            try:
                self._splits = self.feed.splits_today(sorted(self.S.leaves()))
                if self._splits:
                    print(f"!!!! [controller WARN] splits today: "
                          f"{ {self.reg.render(k): v for k, v in self._splits.items()} } "
                          f"— corp_action 标注,shares 以持仓文件为准")
            except Exception as e:  # noqa: BLE001 — 日历失败不阻塞估值
                print(f"!!!! [controller WARN] splits calendar failed: {e}")
                self._splits = {}
            self._splits_date = today_et

        # 3) 快照 → 双引擎 → 对拍
        # 闭市且已有价:不拉快照,平移续写(Robinhood 式匀速推进,零 API 开销,
        # 用户令 2026-08-12)——nav 流每 interval 照常落一行,价格不变即平线;
        # 开市/extended-hours/强制/冷启动 → 正常拉快照。
        stale = False
        fetch = force or (not closed) or not self.last_price
        if fetch:
            try:
                snap = self.feed.snapshot(sorted(self.S.leaves()))
                prices = snap["prices"]
            except Exception as e:  # noqa: BLE001 — 不换源:标 stale 沿用 last_price
                print(f"!!!! [controller WARN] snapshot failed: {e} — tick stale")
                snap = {"feed_delay_min": None, "missing": []}
                prices, stale = {}, True
        else:
            snap = {"feed_delay_min": None, "missing": []}
            prices = {}
        if prices:
            em_f = self.fe.apply_tick(prices)
            em_t = self.te.apply_tick(prices)
            if [n for n, _ in em_f] != [n for n, _ in em_t] or any(
                    abs(a[1] - b[1]) > max(ABS_TOL, REL_TOL * abs(b[1]))
                    for a, b in zip(em_f, em_t)):
                raise EngineDisagreement(f"tick emissions differ:\n{em_f}\n{em_t}")
            self.last_price.update(prices)
        elif fetch:
            # 拉了却空手(如 3:30am ET snapshot 重置窗)= 同 stale 语义;
            # 闭市不拉的平移续写不算 stale(market=closed 已说明一切)
            stale = True
            if not self.last_price:      # 冷启动且全无价:不发近空 nav,不覆盖旧文件
                return {"skipped": "no prices available (snapshot empty, "
                                   "no last_price to carry)", "rebuilt": rebuilt}
        self._verify("tick")
        if time.time() - self.last_rebaseline > REBASELINE_EVERY_S:
            self._rebaseline()

        # 4) 落盘(发布=树引擎值)
        ts = _now_iso()
        values = self.te.values()
        parent_of: dict[str, str] = {}
        for pid, n in self.S.nodes.items():
            for c, _ in n["children"]:
                parent_of[c] = pid
        rows = []
        for nid in self.fe.order:                        # 拓扑序稳定输出
            if nid not in values:
                continue
            n = self.S.nodes[nid]
            exp = self.fe.expanded.get(nid, {})
            corp = any(leaf in self._splits for leaf in exp)
            holdings = None
            if n["kind"] != "portfolio":     # 股票级明细(前端层级展开到叶子)
                holdings = sorted(
                    ({"id": leaf, "name": self.reg.render(leaf),
                      "shares": round(eff, 4),
                      "value": round(eff * self.last_price[leaf], 2)}
                     for leaf, eff in exp.items() if leaf in self.last_price),
                    key=lambda h: -abs(h["value"]))
            rows.append({"node_id": nid,
                         "display_name": self.reg.render(nid),
                         "kind": n["kind"], "value": round(values[nid], 2),
                         "parent_id": parent_of.get(nid),
                         "corp_action": corp,
                         "holdings": holdings,
                         "positions_as_of": n["attrs"].get("positions_as_of")})
        payload = {"ts": ts, "structure_hash": self.S.hash, "stale": stale,
                   "last_rebuild_ts": self.last_rebuild_ts,
                   "structure_diff": self.structure_diff,
                   "corp_actions": {self.reg.render(k): v
                                    for k, v in self._splits.items()},
                   "feed_delay_min": snap.get("feed_delay_min"),
                   "missing": [self.reg.render(m) for m in snap.get("missing", [])],
                   "backfilled": [self.reg.render(b)
                                  for b in snap.get("backfilled", [])],
                   "market": status["market"], "nodes": rows}
        _atomic_json(payload, os.path.join(OUT_DIR, "nav_latest.json"))
        stream = os.path.join(OUT_DIR,
                              f"nav_stream_{datetime.now().strftime('%Y%m%d')}.csv")
        os.makedirs(OUT_DIR, exist_ok=True)
        header = "ts,node_id,display_name,kind,value,stale,corp_action,structure_hash\n"
        if os.path.exists(stream):                      # 旧 schema 文件轮转,不混写
            with open(stream) as fh:
                if fh.readline() != header:
                    os.replace(stream, stream + ".v1")
        new = not os.path.exists(stream)
        with open(stream, "a") as fh:
            if new:
                fh.write(header)
            for r in rows:
                fh.write(f"{ts},{r['node_id']},{r['display_name']},"
                         f"{r['kind']},{r['value']},{int(stale)},"
                         f"{int(r['corp_action'])},{self.S.hash}\n")
        _atomic_json(self.fe.risk_matrix(),
                     os.path.join(OUT_DIR, "risk_matrix_latest.json"))
        root_v = values.get(self.S.root)
        return {"ts": ts, "rebuilt": rebuilt, "stale": stale,
                "portfolio": root_v, "n_nodes": len(rows),
                "feed_delay_min": snap.get("feed_delay_min")}

    # ── 常驻循环 ─────────────────────────────────────────────────────────────
    def run(self, interval_s: int = 60, watch_s: int = 5) -> None:
        print(f"[controller] loop start interval={interval_s}s watch={watch_s}s")
        next_tick = 0.0
        while True:
            now = time.time()
            if _watch_digest() != self.watch_digest:      # 7×24 watcher(盘后也跑)
                self._rebuild(replay=True)
                # 盘后结构变化也要立即反映到 nav_latest(修 C + 用户"马上变")
                self.tick(force=True)
            if now >= next_tick:
                out = self.tick()
                if "skipped" not in out:
                    print(f"[tick] {out['ts']} portfolio="
                          f"{out['portfolio']:,.2f} stale={out['stale']}")
                next_tick = now + interval_s
            time.sleep(watch_s)
