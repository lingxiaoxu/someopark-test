"""exporter — 持仓变更 → 版本化 target → QC ObjectStore(防火墙内唯一出口)。

单向数据流(用户防火墙): 五持仓文件(只读)→ 本模块 → QC。绝无反向。
写入面仅限 trading_quantconnect/state/ 与 QC ObjectStore key mirror/target.json。

用法:
  python exporter.py --golive        一次性: 冻结 legacy + 记 C0 + 首版推送
  python exporter.py --freeze-scaled 一次性: 冻结 S 队列(已按 k 镜像的在手新仓)
  python exporter.py --once          读→若变→推(cron/手动)
  python exporter.py --loop 60       常驻循环(周一测试用前台跑)
  python exporter.py --dry           只算不推(打印 target 摘要)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from inventory_source import (PAIR_STRATEGIES, SOURCE_FILES,   # noqa: E402
                              SourceError, build_target, content_hash,
                              freeze_legacy, golive_scalars, open_pairs,
                              read_snapshot)

STATE_DIR = _THIS_DIR / "state"
LEGACY_PATH = STATE_DIR / "legacy_positions.json"
SCALED_PATH = STATE_DIR / "scaled_positions.json"
EXPORTER_STATE = STATE_DIR / "exporter_state.json"
TARGET_COPY = STATE_DIR / "target_portfolio.json"
RESIDUAL_PATH = STATE_DIR / "fractional_residual.json"
OBJECT_KEY = "mirror/target.json"

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(p: Path, default):
    if p.exists():
        return json.loads(p.read_text())
    return default


def _atomic_write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(p)


def compose(snap) -> dict:
    legacy = _load(LEGACY_PATH, None)
    if legacy is None:
        raise SourceError("no legacy_positions.json — run --golive first "
                          "(镜像绝不在未冻结 legacy 前推送,防误开既有仓)")
    scaled = _load(SCALED_PATH, None)
    if scaled is None:
        # fail-loud: 缺 S 冻结集 ⇒ 在手的 ×k 仓会被当成 m=1 → 明早按新价改仓。
        # 宁可 fail-static(--loop 捕获后保持上版不推)也绝不静默改动既有持仓。
        raise SourceError(f"no {SCALED_PATH.name} — 先跑 --freeze-scaled 冻结"
                          f" S 队列(已按 k 镜像的在手新仓);缺它会把这些仓"
                          f"误判为 m=1 并在次日开盘改仓")
    st = _load(EXPORTER_STATE, {"version": 0, "hash": None,
                                "initial_cash": None})
    built = build_target(snap, legacy=legacy.get("frozen"),
                         prev_residual=_load(RESIDUAL_PATH, {}),
                         scalars=st.get("scalars") or {},
                         scaled=scaled.get("frozen"))
    h = content_hash(built["targets"])
    return {"state": st, "built": built, "hash": h, "legacy": legacy}


def export_once(push: bool = True, force: bool = False) -> dict:
    snap = read_snapshot()
    c = compose(snap)
    st, built, h = c["state"], c["built"], c["hash"]
    changed = h != st.get("hash")
    out = {"changed": changed, "version": st["version"], "hash": h,
           "n_tickers": len(built["targets"]),
           "legacy_alive": {k: len(v) for k, v in
                            built["legacy_alive"].items()},
           "scaled_alive": {k: len(v) for k, v in
                            built["scaled_alive"].items()}}
    if not changed and not force:
        return out

    st["version"] += 1
    st["hash"] = h
    target_doc = {
        "schema": SCHEMA_VERSION,
        "version": st["version"],
        "exported_at": _now(),
        "content_hash": h,
        "initial_cash": st.get("initial_cash"),
        "targets": built["targets"],
        "attribution": built["attribution"],
        "legacy_alive": built["legacy_alive"],
        "scaled_alive": built["scaled_alive"],
    }
    _atomic_write(TARGET_COPY, target_doc)
    _atomic_write(RESIDUAL_PATH, {"as_of": _now(),
                                  "residual": built["residual"]})
    if push:
        from qc_api import QcClient
        c2 = QcClient()
        org = c2.organization_id()
        c2.object_set(org, OBJECT_KEY,
                      json.dumps(target_doc).encode())
        out["pushed"] = True
    _atomic_write(EXPORTER_STATE, st)
    out.update(version=st["version"], changed=True)
    print(f"[exporter] v{st['version']} hash={h} tickers="
          f"{len(built['targets'])} legacy_alive={out['legacy_alive']} "
          f"pushed={push}")
    return out


def golive(push: bool = True) -> dict:
    """一次性: 冻结 legacy(pairs 既有仓)+ 记 C0 + 推首版 target。
    幂等守卫: 已冻结则拒绝重跑(误重跑会把现役新仓错标 legacy)。
    push=False 仅供单测(测试网络禁令: 单测绝不触真实 QC)。"""
    if LEGACY_PATH.exists():
        raise SourceError(f"{LEGACY_PATH} already exists — go-live 只能一次;"
                          f"确要重来先人工归档该文件")
    snap = read_snapshot()
    frozen = freeze_legacy(snap)
    sc = golive_scalars()          # 缩放镜像: 官方/账本 每策略冻结常数
    _atomic_write(LEGACY_PATH, {"frozen_at": _now(), "frozen": frozen})
    # go-live 当刻在手的仓全部落 legacy(m=0) → S 队列天然为空。显式写出来,
    # 因为 compose() 对缺文件是 fail-loud 的:空集合与"忘了冻"必须可区分。
    _atomic_write(SCALED_PATH, {"frozen_at": _now(),
                                "frozen": {st: [] for st in PAIR_STRATEGIES}})
    _atomic_write(EXPORTER_STATE, {"version": 0, "hash": None,
                                   "initial_cash": sc["C0"],
                                   "scalars": sc["scalars"],
                                   "scalar_basis": {"official": sc["official"],
                                                    "ledger": sc["ledger"],
                                                    "frozen_at": _now()}})
    n = {k: len(v) for k, v in frozen.items()}
    print(f"[golive] legacy frozen {n}, C0={sc['C0']:,.2f}, "
          f"scalars={ {k: round(v,3) for k, v in sc['scalars'].items()} }")
    return export_once(push=push, force=True)


def freeze_scaled() -> dict:
    """一次性: 把"当前在手、且不在 legacy 冻结集里"的 pairs 冻进 S 队列。

    这些仓 QC 已按 k 倍建过,用户要求**保持不动**(不补不减,免得按新价改仓)。
    冻结后它们的镜像倍数永久 = k;此刻之后新开的仓落入 F 队列(m=1)。
    幂等守卫: 已存在则拒绝重跑(重跑会把 F 队列的新仓错标成 S,当场改仓)。
    """
    if SCALED_PATH.exists():
        raise SourceError(f"{SCALED_PATH} already exists — S 队列只能冻一次;"
                          f"重跑会把切换后新开的仓错标为 ×k 并当场改仓")
    if not LEGACY_PATH.exists():
        raise SourceError("先 --golive 冻结 legacy,再冻 S 队列")
    snap = read_snapshot()
    legacy = _load(LEGACY_PATH, {}).get("frozen", {})
    frozen: dict[str, list] = {}
    for st in ("mrpt", "mtfs"):
        lset = {(i["pair"], i["open_date"]) for i in legacy.get(st, [])}
        frozen[st] = [{"pair": n, "open_date": info["open_date"]}
                      for n, info in sorted(open_pairs(snap[st]).items())
                      if (n, info["open_date"]) not in lset]
    _atomic_write(SCALED_PATH, {"frozen_at": _now(), "frozen": frozen})
    print(f"[freeze-scaled] S 队列冻结 "
          f"{ {k: [i['pair'] for i in v] for k, v in frozen.items()} }")
    return frozen


def main() -> int:
    ap = argparse.ArgumentParser(description="QC mirror target exporter")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--golive", action="store_true")
    g.add_argument("--freeze-scaled", action="store_true", dest="freeze_scaled")
    g.add_argument("--once", action="store_true")
    g.add_argument("--loop", type=int, metavar="SECONDS")
    g.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if a.golive:
        golive()
        return 0
    if a.freeze_scaled:
        freeze_scaled()
        return 0
    if a.dry:
        snap = read_snapshot()
        if LEGACY_PATH.exists():
            c = compose(snap)
            doc = {"hash": c["hash"], "targets": c["built"]["targets"],
                   "legacy_alive": {k: [i["pair"] for i in v] for k, v in
                                    c["built"]["legacy_alive"].items()},
                   "scaled_alive": {k: [i["pair"] for i in v] for k, v in
                                    c["built"]["scaled_alive"].items()},
                   "residual": c["built"]["residual"]}
        else:
            sc = golive_scalars()
            b = build_target(snap, legacy=freeze_legacy(snap),
                             scalars=sc["scalars"])
            doc = {"note": "PRE-GOLIVE PREVIEW(legacy=当前全部 pairs 实仓;"
                           "缩放镜像=官方口径规模)",
                   "scalars": {k: round(v, 3) for k, v in
                               sc["scalars"].items()},
                   "C0": sc["C0"],
                   "targets": b["targets"], "residual": b["residual"],
                   "would_freeze": {k: len(v) for k, v in
                                    freeze_legacy(snap).items()}}
        print(json.dumps(doc, indent=1, ensure_ascii=False))
        return 0
    if a.once:
        export_once(push=True)
        return 0
    while True:                                   # --loop
        try:
            export_once(push=True)
        except SourceError as e:
            print(f"!!!! [exporter] source error (fail-static, 保持上版): {e}")
        except Exception as e:  # noqa: BLE001 — 循环不死
            print(f"!!!! [exporter] error: {e}")
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
