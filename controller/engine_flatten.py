"""
controller/engine_flatten.py — 方法 1:拍平 + 反向索引 + 扇出(plan §一;M2)。

PDF `run_flatten` 的忠实实现 + plan 修 A(cash 拍平与初始化发出):
  预处理:DFS 后序拍平每个组合 → {leaf: eff_shares};菱形预合并;净 0 删除;
          循环检测;cash 沿持有链拍平;拓扑序(与树引擎共用,保证可逐条对拍)。
  运行时:批量 tick —— Δ 扇出 → seen/required gating → 按拓扑序发出,值变才发。
  required==0 的组合(空仓策略)在 init 时立即定价发出(修 A)。

API(两引擎完全一致,便于 verifier 对拍):
  eng = FlattenEngine(structure)         # structure: model.Structure
  init_emits = eng.initial_emissions()   # 修 A 的初始化发出(拓扑序)
  emits = eng.apply_tick({leaf_id: price, ...})
  eng.values() -> {node_id: value}       # 全层级(仅已集齐的节点)
"""
from __future__ import annotations


class CycleError(RuntimeError):
    pass


def topo_order(structure) -> list[str]:
    """确定性拓扑序(子先于父;children 按 id 排序)。两引擎共用。"""
    nodes = structure.nodes
    order, state = [], {}                      # state: 1=visiting 2=done

    def visit(nid: str):
        if state.get(nid) == 2:
            return
        if state.get(nid) == 1:
            raise CycleError(f"cycle at {nid}")
        state[nid] = 1
        for c, _ in sorted(nodes[nid]["children"]):
            if c in nodes:
                visit(c)
        state[nid] = 2
        order.append(nid)

    for nid in sorted(nodes):
        visit(nid)
    return order


class FlattenEngine:
    name = "flatten"

    def __init__(self, structure):
        self.nodes = structure.nodes
        self.order = topo_order(structure)
        self.pos = {n: i for i, n in enumerate(self.order)}

        # ── 拍平(shares 与 cash 同链)────────────────────────────────────────
        self.expanded: dict[str, dict[str, float]] = {}
        self.cash_flat: dict[str, float] = {}
        for nid in self.order:                       # 拓扑序保证子已算完
            node = self.nodes[nid]
            exp: dict[str, float] = {}
            cash = float(node["attrs"].get("cash_const", 0.0))
            for c, q in node["children"]:
                if c in self.nodes:
                    for leaf, eff in self.expanded[c].items():
                        exp[leaf] = exp.get(leaf, 0.0) + q * eff
                    cash += q * self.cash_flat[c]
                else:
                    exp[leaf_id := c] = exp.get(c, 0.0) + q
            self.expanded[nid] = {k: v for k, v in exp.items() if v != 0.0}
            self.cash_flat[nid] = cash

        # ── 反向索引(leaf → [(pos, portfolio, eff)],父排子后)────────────────
        self.reverse: dict[str, list] = {}
        for nid in self.order:
            for leaf, eff in self.expanded[nid].items():
                self.reverse.setdefault(leaf, []).append((self.pos[nid], nid, eff))
        for leaf in self.reverse:
            self.reverse[leaf].sort()

        # ── 寄存器 ───────────────────────────────────────────────────────────
        self.required = {n: len(self.expanded[n]) for n in self.order}
        self.seen = {n: 0 for n in self.order}
        self.value = {n: self.cash_flat[n] for n in self.order}    # 修 A:cash 起步
        self.fully_priced: set[str] = set()
        self.last_price: dict[str, float] = {}
        self.value_last_emitted: dict[str, float] = {}

    # ── 修 A:初始化发出(required==0 的组合立即定价)──────────────────────────
    def initial_emissions(self) -> list[tuple[str, float]]:
        out = []
        for nid in self.order:
            if self.required[nid] == 0:
                self.fully_priced.add(nid)
                out.append((nid, self.value[nid]))
                self.value_last_emitted[nid] = self.value[nid]
        return out

    # ── 批量 tick ─────────────────────────────────────────────────────────────
    def apply_tick(self, prices: dict[str, float]) -> list[tuple[str, float]]:
        touched: set[str] = set()
        for leaf, price in prices.items():
            fanout = self.reverse.get(leaf)
            first = leaf not in self.last_price
            delta = price if first else price - self.last_price[leaf]
            self.last_price[leaf] = price
            if not fanout:
                continue                                  # 不在任何组合,只记价
            for _, nid, eff in fanout:
                if first:
                    self.seen[nid] += 1
                    if self.seen[nid] == self.required[nid]:
                        self.fully_priced.add(nid)
                self.value[nid] += eff * delta
                touched.add(nid)
        emits = []
        for nid in sorted(touched, key=self.pos.get):
            if nid not in self.fully_priced:
                continue
            v = self.value[nid]
            if nid not in self.value_last_emitted or v != self.value_last_emitted[nid]:
                emits.append((nid, v))
                self.value_last_emitted[nid] = v
        return emits

    def values(self) -> dict[str, float]:
        return {n: self.value[n] for n in self.order if n in self.fully_priced}

    def risk_matrix(self) -> dict[str, list]:
        """反向索引即风险矩阵(leaf → [(portfolio, eff_exposure)])。"""
        return {leaf: [(nid, eff) for _, nid, eff in rows]
                for leaf, rows in self.reverse.items()}
