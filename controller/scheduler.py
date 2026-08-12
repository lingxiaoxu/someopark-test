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
from controller.prices import PriceFeed

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


class Controller:
    def __init__(self):
        self.reg = Registry()
        self.feed = PriceFeed(self.reg)
        self.last_price: dict[str, float] = {}
        self.watch_digest: str | None = None
        self.last_rebaseline = 0.0
        self._rebuild(replay=False)

    # ── 结构(重)建 + 合成重放(修 C 统一语义)────────────────────────────────
    def _rebuild(self, replay: bool = True) -> None:
        self.S = assemble(self.reg)
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
        if status["market"] == "closed" and not force:
            return {"skipped": "market closed", "rebuilt": rebuilt}

        # 3) 快照 → 双引擎 → 对拍
        stale = False
        try:
            snap = self.feed.snapshot(sorted(self.S.leaves()))
            prices = snap["prices"]
        except Exception as e:  # noqa: BLE001 — 不换源:标 stale 沿用 last_price
            print(f"!!!! [controller WARN] snapshot failed: {e} — tick stale")
            snap = {"feed_delay_min": None, "missing": []}
            prices, stale = {}, True
        if prices:
            em_f = self.fe.apply_tick(prices)
            em_t = self.te.apply_tick(prices)
            if [n for n, _ in em_f] != [n for n, _ in em_t] or any(
                    abs(a[1] - b[1]) > max(ABS_TOL, REL_TOL * abs(b[1]))
                    for a, b in zip(em_f, em_t)):
                raise EngineDisagreement(f"tick emissions differ:\n{em_f}\n{em_t}")
            self.last_price.update(prices)
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
            rows.append({"node_id": nid,
                         "display_name": self.reg.render(nid),
                         "kind": n["kind"], "value": round(values[nid], 2),
                         "parent_id": parent_of.get(nid),
                         "positions_as_of": n["attrs"].get("positions_as_of")})
        payload = {"ts": ts, "structure_hash": self.S.hash, "stale": stale,
                   "feed_delay_min": snap.get("feed_delay_min"),
                   "missing": [self.reg.render(m) for m in snap.get("missing", [])],
                   "market": status["market"], "nodes": rows}
        _atomic_json(payload, os.path.join(OUT_DIR, "nav_latest.json"))
        stream = os.path.join(OUT_DIR,
                              f"nav_stream_{datetime.now().strftime('%Y%m%d')}.csv")
        os.makedirs(OUT_DIR, exist_ok=True)
        new = not os.path.exists(stream)
        with open(stream, "a") as fh:
            if new:
                fh.write("ts,node_id,display_name,kind,value,stale\n")
            for r in rows:
                fh.write(f"{ts},{r['node_id']},{r['display_name']},"
                         f"{r['kind']},{r['value']},{int(stale)}\n")
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
