"""inventory_source — 五策略持仓文件直读 → 净额目标(纯函数核心,零仓库内 import)。

防火墙(用户规格): 下单链路的输入仅限五个持仓文件(只读)+ API 凭证。
本模块绝不写任何仓库路径,绝不 import 仓库其他模块。

每策略"股票层可执行真源"(plan §1.1):
  mrpt/mtfs: inventory_{s}.json → pairs.{P}(direction≠null)的 s1/s2_shares
  aiss     : account_aiss.json → positions(inventory_aiss 是子板块合成层,不可执行)
  ssrs     : inventory_sector_rotation.json → holdings(ETF 层即可执行层)
  bdc      : inventory_bdc.json → holdings + cash(BIL 持仓;小数股→整数+残差)

半更新窗/撕裂读对策(v2,自证): 每策略单文件自洽 → 双读稳定判定
(两次读间隔 settle_ms,内容哈希一致才采纳)+ 解析失败重试;绝不采信单次读。

Legacy(冷启动,plan §9): 只减不增的冻结集 {pair, open_date};存在性与腿数
来自同一次 json 读取 → 同快照原子。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
REPO = _THIS_DIR.parent

SOURCE_FILES = {
    "mrpt": REPO / "inventory_mrpt.json",
    "mtfs": REPO / "inventory_mtfs.json",
    "aiss": REPO / "qlib-main/semiconductor_strategy/account_aiss.json",
    "ssrs": REPO / "qlib-main/sector_rotation/inventory_sector_rotation.json",
    "bdc":  REPO / "inventory_bdc.json",
}

PAIR_STRATEGIES = ("mrpt", "mtfs")


class SourceError(RuntimeError):
    """读取/校验失败 —— 调用方保持上一版 target(fail-static),绝不猜。"""


# ── 稳定读取 ────────────────────────────────────────────────────────────────

def stable_read(path: Path, settle_ms: int = 300, attempts: int = 5) -> dict:
    """双读稳定判定: 连续两次读(间隔 settle_ms)字节一致才接受。
    覆盖 DailySignal 非原子写的撕裂窗;attempts 内不稳定 → SourceError。"""
    last_err = None
    prev = None
    for _ in range(attempts):
        try:
            b1 = path.read_bytes()
            time.sleep(settle_ms / 1000.0)
            b2 = path.read_bytes()
        except OSError as e:
            last_err = e
            time.sleep(settle_ms / 1000.0)
            continue
        if b1 == b2:
            try:
                return json.loads(b2)
            except json.JSONDecodeError as e:   # 写到一半的 JSON
                last_err = e
                time.sleep(settle_ms / 1000.0)
                continue
        prev = e = None  # noqa: F841 — 不稳定,再来
    raise SourceError(f"unstable/corrupt read: {path} ({last_err})")


# ── 每策略股票层提取(纯函数: dict → {ticker: shares})──────────────────────

def pairs_positions(inv: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, v in (inv.get("pairs") or {}).items():
        if not isinstance(v, dict) or not v.get("direction"):
            continue                             # 候选/化石槽(direction=null)
        legs = name.split("/")
        if len(legs) != 2:
            raise SourceError(f"unparseable pair name {name!r}")
        s1, s2 = v.get("s1_shares"), v.get("s2_shares")
        if s1 is None or s2 is None:
            raise SourceError(f"open pair {name} missing shares")
        out[legs[0]] = out.get(legs[0], 0.0) + float(s1)
        out[legs[1]] = out.get(legs[1], 0.0) + float(s2)
    return out


def open_pairs(inv: dict) -> dict[str, dict]:
    """{pair_name: {open_date, legs:{ticker: shares}}}(仅实仓)。"""
    out = {}
    for name, v in (inv.get("pairs") or {}).items():
        if not isinstance(v, dict) or not v.get("direction"):
            continue
        legs = name.split("/")
        out[name] = {"open_date": v.get("open_date"),
                     "legs": {legs[0]: float(v["s1_shares"]),
                              legs[1]: float(v["s2_shares"])}}
    return out


def account_positions(acc: dict) -> dict[str, float]:
    out = {}
    for t, p in (acc.get("positions") or {}).items():
        sh = p.get("shares") if isinstance(p, dict) else p
        if sh is None:
            raise SourceError(f"position {t} missing shares")
        out[t] = float(sh)
    return out


def holdings_positions(inv: dict) -> dict[str, float]:
    out = {}
    for t, h in (inv.get("holdings") or {}).items():
        sh = h.get("shares") if isinstance(h, dict) else h
        if sh is None:
            raise SourceError(f"holding {t} missing shares")
        out[t] = float(sh)
    return out


def bdc_positions(inv: dict) -> dict[str, float]:
    out = holdings_positions(inv)
    cash = inv.get("cash") or {}
    if cash.get("ticker"):
        out[cash["ticker"]] = out.get(cash["ticker"], 0.0) + float(cash["shares"])
    return out


EXTRACTORS = {
    "mrpt": pairs_positions,
    "mtfs": pairs_positions,
    "aiss": account_positions,
    "ssrs": holdings_positions,
    "bdc":  bdc_positions,
}


# ── 快照 → 目标(净额 + legacy 减法 + BDC 整数化残差)────────────────────────

def read_snapshot(files: dict[str, Path] | None = None) -> dict[str, dict]:
    files = files or SOURCE_FILES
    snap = {}
    for st, p in files.items():
        if not p.exists():
            raise SourceError(f"source file missing: {p}")
        snap[st] = stable_read(p)
    return snap


def build_target(snap: dict[str, dict],
                 legacy: dict[str, list] | None = None,
                 prev_residual: dict[str, float] | None = None) -> dict:
    """→ {targets:{ticker:int}, attribution, legacy_alive, residual, meta_src}
    纯函数(无 IO): 单测直接钉。

    legacy: {"mrpt":[{"pair","open_date"}],...} 冻结集;存活=同名+同 open_date
            (同一次 snap 读取内判定 → 与腿数同快照)。
    BDC 小数: 目标 = floor/ceil 至最近整数(round half-even),
            residual[t] = 真实小数股 − 整数目标(本地记账,QC 只见整数)。
    """
    legacy = legacy or {}
    per_strategy: dict[str, dict[str, float]] = {}
    for st, raw in snap.items():
        per_strategy[st] = EXTRACTORS[st](raw)

    # legacy 减法(pairs 域): 存活判定与减项同源于 snap
    # legacy_alive 恒含全部冻结策略键(0 也显式 —— 过渡期报表要能看到清零)
    legacy_alive: dict[str, list] = {st: [] for st in legacy
                                     if st in PAIR_STRATEGIES}
    for st in PAIR_STRATEGIES:
        alive_pairs = open_pairs(snap[st]) if st in snap else {}
        for item in legacy.get(st, []):
            cur = alive_pairs.get(item["pair"])
            if cur and cur.get("open_date") == item.get("open_date"):
                legacy_alive[st].append(item)
                for t, sh in cur["legs"].items():
                    per_strategy[st][t] = per_strategy[st].get(t, 0.0) - sh
        # 清理减成 0 的键
        per_strategy[st] = {t: v for t, v in per_strategy[st].items()
                            if abs(v) > 1e-9}

    # 净额扁平化 + 归因
    net: dict[str, float] = {}
    attribution: dict[str, dict[str, float]] = {}
    for st, pos in per_strategy.items():
        for t, sh in pos.items():
            net[t] = net.get(t, 0.0) + sh
            attribution.setdefault(t, {})[st] = sh

    # 整数化: 非 BDC 票必须本来就是整数(pairs/aiss/ssrs 皆整数股,
    # 非整=数据异常,大声拒绝);BDC 小数 → 残差账
    targets: dict[str, int] = {}
    residual: dict[str, float] = {}
    bdc_tickers = set(per_strategy.get("bdc", {}))
    for t, sh in net.items():
        if abs(sh - round(sh)) < 1e-6:
            q = int(round(sh))
        elif t in bdc_tickers:
            q = int(round(sh))                   # banker 舍入到整数
            residual[t] = round(sh - q, 6)
        else:
            raise SourceError(f"non-integer shares for non-BDC ticker {t}: {sh}")
        if q != 0:
            targets[t] = q
    _ = prev_residual                            # 留接口: 残差>1股并入(M4)
    return {"targets": targets,
            "attribution": {t: a for t, a in attribution.items()},
            "legacy_alive": legacy_alive,
            "residual": residual}


def content_hash(targets: dict[str, int]) -> str:
    blob = json.dumps(targets, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def freeze_legacy(snap: dict[str, dict]) -> dict[str, list]:
    """go-live 一次性: 当前 pairs 实仓 → 冻结集(只减不增)。"""
    out: dict[str, list] = {}
    for st in PAIR_STRATEGIES:
        out[st] = [{"pair": name, "open_date": info["open_date"]}
                   for name, info in sorted(open_pairs(snap[st]).items())]
    return out


def initial_cash(files: dict[str, Path] | None = None) -> float:
    """C0 = Σ 五策略 account equity(go-live 一次性读;BDC 用 account_bdc)。"""
    repo = (files or SOURCE_FILES)["mrpt"].parent
    paths = [repo / "account_mrpt.json", repo / "account_mtfs.json",
             repo / "qlib-main/semiconductor_strategy/account_aiss.json",
             repo / "qlib-main/sector_rotation/account_ssrs.json",
             repo / "account_bdc.json"]
    tot = 0.0
    for p in paths:
        d = stable_read(p)
        eq = d.get("equity")
        if eq is None:
            raise SourceError(f"{p.name}: equity missing")
        tot += float(eq)
    return round(tot, 2)
