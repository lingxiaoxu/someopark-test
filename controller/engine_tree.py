"""
controller/engine_tree.py — 方法 2:保留树 + 变化上推(plan §一;M2)。

PDF `run_tree` 的忠实实现 + plan 修 A(cash 初始化与空节点初始发出):
  预处理:held_directly_by 反向父边;与拍平引擎**共用同一拓扑序**(可逐条对拍);
          still_waiting_for[P] = 直接持有数(叶子+子组合)。
  运行时:价格 Δ 更新直接父 → heap(按拓扑位,先深后浅)上推;
          diamond 两路累加、每轮只发一次;值变才发;发出把 change 传给父。
  required==0(children 空,如空仓 MRPT)在 init 轮视为已定价并向父传播(修 A)。

API 与 FlattenEngine 完全一致。
"""
from __future__ import annotations

import heapq

from controller.engine_flatten import topo_order


class TreeEngine:
    name = "tree"

    def __init__(self, structure):
        self.nodes = structure.nodes
        self.order = topo_order(structure)
        self.pos = {n: i for i, n in enumerate(self.order)}

        # 反向父边:任何东西(叶子或组合)→ [(直接父, q)]
        self.held_by: dict[str, list] = {}
        for nid in self.order:
            for c, q in self.nodes[nid]["children"]:
                self.held_by.setdefault(c, []).append((nid, q))

        self.waiting = {n: len(self.nodes[n]["children"]) for n in self.order}
        self.total = {n: float(self.nodes[n]["attrs"].get("cash_const", 0.0))
                      for n in self.order}                 # 修 A:own cash 起步
        self.priced: set[str] = set()                      # 已首发的组合
        self.last_price: dict[str, float] = {}
        self.value_last_emitted: dict[str, float] = {}

    # ── 内部:一轮 heap 上推(prime 集合 = 本轮被触及的组合)────────────────────
    def _pump(self, heap: list, in_heap: set) -> list[tuple[str, float]]:
        emits, done_this_round = [], set()
        while heap:
            _, nid = heapq.heappop(heap)
            if nid in done_this_round:
                continue
            if self.waiting[nid] != 0:
                continue                                   # gating:还差成分
            done_this_round.add(nid)
            v = self.total[nid]
            first = nid not in self.priced
            changed = first or v != self.value_last_emitted.get(nid)
            if not changed:
                continue
            emits.append((nid, v))
            change = v if first else v - self.value_last_emitted[nid]
            self.priced.add(nid)
            self.value_last_emitted[nid] = v
            for parent, q in self.held_by.get(nid, ()):    # 变化上推
                self.total[parent] += q * change
                if first:
                    self.waiting[parent] -= 1
                if parent not in in_heap:
                    heapq.heappush(heap, (self.pos[parent], parent))
                    in_heap.add(parent)
        return emits

    # ── 修 A:初始化发出(children 空的组合立即定价并向父传播)──────────────────
    def initial_emissions(self) -> list[tuple[str, float]]:
        heap, in_heap = [], set()
        for nid in self.order:
            if self.waiting[nid] == 0:                     # 空 children(含空仓策略)
                heapq.heappush(heap, (self.pos[nid], nid))
                in_heap.add(nid)
        return self._pump(heap, in_heap)

    # ── 批量 tick ─────────────────────────────────────────────────────────────
    def apply_tick(self, prices: dict[str, float]) -> list[tuple[str, float]]:
        heap, in_heap = [], set()
        for leaf, price in prices.items():
            parents = self.held_by.get(leaf)
            first = leaf not in self.last_price
            delta = price if first else price - self.last_price[leaf]
            self.last_price[leaf] = price
            if not parents:
                continue
            for parent, q in parents:
                self.total[parent] += q * delta
                if first:
                    self.waiting[parent] -= 1
                if parent not in in_heap:
                    heapq.heappush(heap, (self.pos[parent], parent))
                    in_heap.add(parent)
        return self._pump(heap, in_heap)

    def values(self) -> dict[str, float]:
        return {n: self.total[n] for n in self.order if n in self.priced}
