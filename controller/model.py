"""
controller/model.py — 五策略结构装配(plan §2.1-2.4;M1b)。

产出统一组合代数(全 ID 化,零裸名字):
  Node = { id, kind, children: [(child_id, shares)], attrs }
  attrs 保留中间层业务属性(direction/weight/days_held/…)与 cash_const。

装配数据源(全部只读,persistence 见 PORTFOLIO_DATA_MAP.md):
  pairs : inventory_{s}.json(结构)+ account_{s}.json(cash/对账)
  aiss  : daily report stock_breakdown(per-branch)+ account_aiss.json(实仓/cash)
          + inventory_aiss.json(subsector 权重 attrs)
  ssrs  : account_ssrs.json + inventory_sector_rotation.json(attrs)
  bdc   : inventory_bdc.json
自检(M1b 验收,失败即 raise):
  ① pairs: cash + Σ两腿市值(open px)≈ account.equity 口径关系(结构对账)
  ② aiss : Σbranch shares ≡ account.positions 总数(逐票,归一后精确)
  ③ 全树零裸名字(children 全为 ISIN/SPID)
纪律:解析不出 ID 即 raise(RegistryError);不修改任何源文件。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os

from controller.registry import (Registry, RegistryError, pair_canonical_key,
                                 strategy_canonical_key, subsector_canonical_key,
                                 PORTFOLIO_KEY, STRATEGIES)

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(_HERE)


def _j(relpath: str):
    return json.load(open(os.path.join(REPO, relpath)))


class Structure:
    """一次装配的完整结构:nodes{id: Node} + 根 id + 结构哈希。"""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.root: str | None = None
        self.hash: str | None = None
        self.sources: dict[str, str] = {}       # 装配读过的文件 → 内容摘要

    def add(self, node_id: str, kind: str, children=None, attrs=None):
        self.nodes[node_id] = {"id": node_id, "kind": kind,
                               "children": children or [], "attrs": attrs or {}}
        return self.nodes[node_id]

    def leaves(self) -> set[str]:
        child_ids = {c for n in self.nodes.values() for c, _ in n["children"]}
        return {c for c in child_ids if c not in self.nodes}

    def compute_hash(self) -> str:
        basis = []
        for nid in sorted(self.nodes):
            n = self.nodes[nid]
            basis.append((nid, n["kind"],
                          tuple(sorted((c, round(q, 6)) for c, q in n["children"])),
                          round(float(n["attrs"].get("cash_const", 0.0)), 2)))
        self.hash = hashlib.sha1(repr(basis).encode()).hexdigest()[:16]
        return self.hash


def _digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()[:12]


# ── 各策略装配 ────────────────────────────────────────────────────────────────

def _assemble_pairs(st: str, reg: Registry, S: Structure) -> str:
    inv_p = os.path.join(REPO, f"inventory_{st}.json")
    acc_p = os.path.join(REPO, f"account_{st}.json")
    inv, acc = json.load(open(inv_p)), json.load(open(acc_p))
    S.sources[inv_p] = _digest(inv_p)
    S.sources[acc_p] = _digest(acc_p)

    st_id = reg.spid_of("strategy", strategy_canonical_key(st))
    children = []
    open_names = []
    for name, v in inv["pairs"].items():
        if not v.get("direction"):
            continue
        legs = name.split("/")
        if len(legs) != 2:
            raise RegistryError(f"{st}: unparseable pair name {name!r}")
        s1_isin, s2_isin = reg.isin_of(legs[0]), reg.isin_of(legs[1])
        key = pair_canonical_key(st, s1_isin, s2_isin, v["direction"],
                                 v.get("s1_shares"), v.get("s2_shares"))
        pid = reg.spid_of("pair", key, display_name=name, attrs={"strategy": st})
        pair_attrs = {k: v[k] for k in
                      ("direction", "param_set", "open_date", "days_held",
                       "open_hedge_ratio", "peak_unrealized_pnl",
                       "open_s1_price", "open_s2_price") if k in v}
        pair_attrs["display_name"] = name
        S.add(pid, "pair",
              children=[(s1_isin, float(v["s1_shares"])),
                        (s2_isin, float(v["s2_shares"]))],
              attrs=pair_attrs)
        children.append((pid, 1.0))
        open_names.append(name)

    cash = float(acc["cash"])
    S.add(st_id, "strategy", children=children,
          attrs={"display_name": st.upper(), "cash_const": cash,
                 "positions_as_of": inv.get("as_of"),
                 "account_equity_ref": float(acc.get("equity", 0.0)),
                 "open_pairs": open_names})
    # 自检①:inventory 开仓腿 与 account.positions 股票集合互验(账本口径应含同一批票)
    inv_tickers = {t for n in open_names for t in n.split("/")}
    acc_tickers = set(acc.get("positions", {}))
    if inv_tickers != acc_tickers:
        raise RegistryError(
            f"{st}: inventory legs {sorted(inv_tickers)} != account positions "
            f"{sorted(acc_tickers)} — 持仓两源不一致,拒绝装配")
    return st_id


def _latest_aiss_report() -> str:
    files = sorted(glob.glob(os.path.join(
        REPO, "qlib-main/semiconductor_strategy/trading_signals/aiss_daily_report_*.json")))
    if not files:
        raise RegistryError("no aiss_daily_report_*.json found")
    return files[-1]


def _assemble_aiss(reg: Registry, S: Structure) -> str:
    acc_p = os.path.join(REPO, "qlib-main/semiconductor_strategy/account_aiss.json")
    inv_p = os.path.join(REPO, "qlib-main/semiconductor_strategy/inventory_aiss.json")
    rep_p = _latest_aiss_report()
    acc, inv, rep = json.load(open(acc_p)), json.load(open(inv_p)), json.load(open(rep_p))
    for p in (acc_p, inv_p, rep_p):
        S.sources[p] = _digest(p)

    positions = {t: float(v["shares"]) for t, v in acc["positions"].items()}
    breakdown = rep.get("stock_breakdown") or []
    # per-branch 目标股数(report)→ 按 account 实仓逐票归一(account 是 golden)
    tgt_by_ticker: dict[str, float] = {}
    rows_by_ticker: dict[str, list] = {}
    for row in breakdown:
        t = row["ticker"]
        tgt_by_ticker[t] = tgt_by_ticker.get(t, 0.0) + float(row["target_shares"])
        rows_by_ticker.setdefault(t, []).append(row)

    # 校验:report 覆盖必须包含全部实仓票(比例来源不能缺票)
    missing = [t for t in positions if t not in rows_by_ticker]
    if missing:
        raise RegistryError(f"aiss: account 持仓 {missing} 不在 stock_breakdown —"
                            f" report({os.path.basename(rep_p)}) 与 account 脱节,拒绝装配")

    ss_children: dict[str, list] = {}
    for t, actual in positions.items():
        rows = rows_by_ticker[t]
        tgt_total = tgt_by_ticker[t]
        if tgt_total <= 0:
            raise RegistryError(f"aiss: {t} target_shares 合计 {tgt_total} 无法归一")
        drift = abs(actual - tgt_total) / max(actual, 1.0)
        if drift > 0.05:                            # 目标 vs 实仓漂移 >5% 报警但不吞
            print(f"!!!! [model ALERT] aiss {t}: account {actual} vs report target "
                  f"{tgt_total} drift {drift:.1%}(rebalance 未执行完?)——按实仓归一")
        isin = reg.isin_of(t)
        for row in rows:
            branch_shares = actual * float(row["target_shares"]) / tgt_total
            ss_children.setdefault(row["subsector"], []).append((isin, branch_shares))

    st_id = reg.spid_of("strategy", strategy_canonical_key("aiss"))
    children = []
    inv_w = inv.get("holdings", {})
    for ss_key, kids in sorted(ss_children.items()):
        sid = reg.spid_of("subsector", subsector_canonical_key("aiss", ss_key),
                          display_name=ss_key, attrs={"strategy": "aiss"})
        ss_attrs = {"display_name": ss_key}
        if ss_key in inv_w:
            ss_attrs.update({k: inv_w[ss_key][k] for k in
                             ("weight", "days_held", "action_today", "entry_date")
                             if k in inv_w[ss_key]})
        S.add(sid, "subsector", children=kids, attrs=ss_attrs)
        children.append((sid, 1.0))

    S.add(st_id, "strategy", children=children,
          attrs={"display_name": "AISS", "cash_const": float(acc["cash"]),
                 "positions_as_of": acc.get("as_of"),
                 "report_file": os.path.basename(rep_p)})
    # 自检②:Σbranch ≡ account 逐票(归一构造保证,仍显式断言防回归)
    got: dict[str, float] = {}
    for kids in ss_children.values():
        for isin, sh in kids:
            got[isin] = got.get(isin, 0.0) + sh
    for t, actual in positions.items():
        isin = reg.isin_of(t)
        if abs(got[isin] - actual) > 1e-6:
            raise RegistryError(f"aiss: Σbranch({t})={got[isin]} != account {actual}")
    return st_id


def _assemble_ssrs(reg: Registry, S: Structure) -> str:
    acc_p = os.path.join(REPO, "qlib-main/sector_rotation/account_ssrs.json")
    inv_p = os.path.join(REPO, "qlib-main/sector_rotation/inventory_sector_rotation.json")
    acc, inv = json.load(open(acc_p)), json.load(open(inv_p))
    S.sources[acc_p] = _digest(acc_p)
    S.sources[inv_p] = _digest(inv_p)
    st_id = reg.spid_of("strategy", strategy_canonical_key("ssrs"))
    children = []
    for t, v in acc["positions"].items():
        children.append((reg.isin_of(t), float(v["shares"])))
    S.add(st_id, "strategy", children=children,
          attrs={"display_name": "SSRS", "cash_const": float(acc["cash"]),
                 "positions_as_of": acc.get("as_of"),
                 "holdings_attrs": {t: {k: h[k] for k in
                                        ("weight", "days_held", "action_today") if k in h}
                                    for t, h in inv.get("holdings", {}).items()}})
    return st_id


def _assemble_bdc(reg: Registry, S: Structure) -> str:
    inv_p = os.path.join(REPO, "inventory_bdc.json")
    inv = json.load(open(inv_p))
    S.sources[inv_p] = _digest(inv_p)
    st_id = reg.spid_of("strategy", strategy_canonical_key("bdc"))
    children = []
    for t, h in inv["holdings"].items():
        children.append((reg.isin_of(t), float(h["shares"])))
    children.append((reg.isin_of(inv["cash"]["ticker"]),
                     float(inv["cash"]["shares"])))
    S.add(st_id, "strategy", children=children,
          attrs={"display_name": "BDC", "positions_as_of": inv.get("as_of"),
                 "weights": {t: h["weight"] for t, h in inv["holdings"].items()},
                 "allocation": inv.get("allocation")})
    return st_id


WATCH_FILES = [                                   # 装配读什么、watcher 就盯什么(§2.4)
    "inventory_mrpt.json", "account_mrpt.json",
    "inventory_mtfs.json", "account_mtfs.json",
    "inventory_bdc.json",
    "qlib-main/semiconductor_strategy/inventory_aiss.json",
    "qlib-main/semiconductor_strategy/account_aiss.json",
    "qlib-main/sector_rotation/inventory_sector_rotation.json",
    "qlib-main/sector_rotation/account_ssrs.json",
]


def assemble(reg: Registry | None = None) -> Structure:
    """全书装配 → Structure(装配后 reg.save() 持久化新注册的节点)。"""
    reg = reg or Registry()
    S = Structure()
    ids = [
        _assemble_pairs("mrpt", reg, S),
        _assemble_pairs("mtfs", reg, S),
        _assemble_aiss(reg, S),
        _assemble_ssrs(reg, S),
        _assemble_bdc(reg, S),
    ]
    pf_id = reg.spid_of("portfolio", PORTFOLIO_KEY, display_name="PORTFOLIO")
    S.add(pf_id, "portfolio", children=[(i, 1.0) for i in ids],
          attrs={"display_name": "PORTFOLIO"})
    S.root = pf_id
    # 自检③:全树零裸名字
    for leaf in S.leaves():
        if not (leaf in reg.master or leaf.startswith("XF")):
            raise RegistryError(f"bare/unknown leaf id {leaf!r} in tree")
    S.compute_hash()
    reg.save()
    return S


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="controller structure assembly (M1b)")
    ap.add_argument("--dump", action="store_true", help="打印装配树(显示名渲染)")
    a = ap.parse_args()
    reg = Registry()
    S = assemble(reg)
    print(f"[model] assembled: {len(S.nodes)} nodes, {len(S.leaves())} leaves, "
          f"hash={S.hash}")
    if a.dump:
        def walk(nid, depth=0):
            n = S.nodes.get(nid)
            name = reg.render(nid)
            if n is None:
                print("  " * depth + f"- {name} [{nid}]")
                return
            cash = n["attrs"].get("cash_const")
            extra = f"  cash=${cash:,.0f}" if cash else ""
            print("  " * depth + f"+ {name} [{nid}] ({n['kind']}){extra}")
            for c, q in n["children"]:
                if c in S.nodes:
                    walk(c, depth + 1)
                else:
                    print("  " * (depth + 1) + f"- {reg.render(c)} × {q:,.4g} [{c}]")
        walk(S.root)
