"""Forward-method epoch update: the standard path for changing runtime-pinned code.

Any edit under util/ model/ strategy/ config/ ingest/ venues/ jobs/ exec/ (or the
named ops files, or the identity files hashed by identity_manifest_id) changes the
runtime fingerprint the active epoch pins, so the paper path refuses to trade until
a NEW epoch carrying the new fingerprint is activated. This tool is that path.

Method rules never change here — an epoch update is for code/parameter fingerprint
changes only. The previous epoch (and everything it vouched for) goes into the new
manifest's compatible set, so forward entries recorded under it keep feeding PIT
calibration (paper_trading reads only the ACTIVE epoch's compatible list).

Run with the three soccer launchd services unloaded:
    python -m ...ops.forward_epoch_update precheck
    python -m ...ops.forward_epoch_update activate --method-version <label> --durable <dirname>
    python -m ...ops.forward_epoch_update verify   --method-version <label>

History: the 2026-09-12 epochs (demo-mirror Decimal fix; evidence tiers recompute)
were activated by this tool's one-shot predecessors; their durable roots live under
data/book_candidates/epoch_v8_20260912 and epoch_v9_20260912.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
MOD = REPO / "prediction_market_soccer"
FRONTEND = REPO / "someo-park-investment-management" / "public" / "data" / "soccer"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _conn():
    from prediction_market_soccer.ingest import store
    return store.init_db()


def precheck(args) -> None:
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.util.frozen_strategy_store import _require_no_open_positions, active_version
    from prediction_market_soccer.ops.version_workflow import _pending_completion
    conn = _conn()
    live = conn.execute(
        "SELECT COUNT(*) FROM fixture WHERE status_short IN ('1H','2H','HT','ET','P','BT')").fetchone()[0]
    assert live == 0, f"{live} matches in progress"
    _require_no_open_positions(conn); print("未平仓位: 无 ✓")
    _pending_completion(conn); print("未追加 completion: 无 ✓")
    print("激活账本:", active_version(conn)["version_id"][:12], "✓")
    ep = FM.active_epoch(conn)
    assert ep, "无激活 epoch"
    drift = ep["manifest"]["runtime"] != FM.runtime_snapshot()
    print(f"当前 epoch {ep['epoch_id'][:12]} ({ep['method_version']}),runtime 已漂移: {drift}")
    assert drift, "运行时未变——没有要登记的新指纹"
    marker = REPO / ".soccer-isolated"
    assert marker.exists() and marker.read_text().strip() == "soccer-isolated-v1", "隔离标记缺失"
    import subprocess
    for proc in ("live_refresh", "refresh_all", "settle_reports"):
        r = subprocess.run(["pgrep", "-f", f"prediction_market_soccer.ops.{proc}"],
                           capture_output=True, text=True)
        assert not r.stdout.strip(), f"{proc} 仍在跑——先卸载服务"
    print("precheck ✓")


def activate(args) -> None:
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.ops import version_workflow as VW
    conn = _conn()
    cur = FM.active_epoch(conn)
    assert cur, "无激活 epoch"
    # Transitive closure: the new epoch vouches for the old one AND everything it vouched for.
    compatible = [cur["epoch_id"], *cur["manifest"]["compatible_epoch_ids"]]
    manifest = FM.build_manifest(model_version=cur["model_version"],
                                 method_version=args.method_version,
                                 compatible_epoch_ids=compatible)
    assert manifest["rules"] == cur["manifest"]["rules"], \
        "规则变了——这不是 epoch update 能做的事,走完整的账本版本流程"
    dur = MOD / "data" / "book_candidates" / args.durable
    dur.mkdir(parents=True, exist_ok=True)
    (dur / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    dirs = [MOD / "data" / "output", FRONTEND]
    # activate_forward_method creates recovery/ itself (exist_ok=False — do not pre-create);
    # an interrupted attempt leaves its operation_id fsynced in the maintenance marker: reuse it.
    stop = MOD / "data" / ".soccer-maintenance.json"
    operation_id = f"epoch-update-{_now()}"
    if stop.exists():
        prior = json.loads(stop.read_text()).get("operation_id")
        if prior:
            operation_id = prior
            print("复用残留 operation_id:", operation_id)
    VW.activate_forward_method(conn, root=REPO, directories=dirs, recovery_dir=dur / "recovery",
                               operation_id=operation_id,
                               expected_head=VW.head(conn, dirs), manifest=manifest)
    ep = FM.active_epoch(conn)
    print("activate ✓ epoch", ep["epoch_id"][:12], "|", ep["method_version"],
          "| 绑定", ep["book_version_id"][:12])


def verify(args) -> None:
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    conn = _conn()
    ep = FM.active_epoch(conn)
    assert ep["method_version"] == args.method_version, ep["method_version"]
    assert ep["manifest"]["runtime"] == FM.runtime_snapshot(), "新 epoch 指纹与当前代码不一致"
    assert ep["book_version_id"] == active_version(conn)["version_id"], "epoch 未绑定激活账本"
    assert ep["manifest"]["compatible_epoch_ids"], "兼容集为空——旧前向入场会掉出 PIT 校准"
    print("epoch:", ep["epoch_id"][:12], ep["method_version"], "| runtime 一致 ✓ | 兼容",
          len(ep["manifest"]["compatible_epoch_ids"]), "个旧 epoch | 绑定", ep["book_version_id"][:12])
    from prediction_market_soccer.ops import paper_trading
    out = paper_trading.run_cycle(conn)
    assert not out.get("errors"), out
    print("paper run_cycle:", out.get("state", "idle"), out.get("errors", []))
    from prediction_market_soccer.exec import kalshi_mirror as km
    res = km.run_cycle(conn)
    assert not res.get("errors"), res
    print("kalshi mirror run_cycle:", res.get("summary", res))
    print("verify ✓")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["precheck", "activate", "verify"])
    ap.add_argument("--method-version", help="new epoch's method_version label")
    ap.add_argument("--durable", help="dirname under data/book_candidates/ for manifest+recovery")
    args = ap.parse_args()
    if args.step in ("activate", "verify") and not args.method_version:
        sys.exit("--method-version required")
    if args.step == "activate" and not args.durable:
        sys.exit("--durable required")
    {"precheck": precheck, "activate": activate, "verify": verify}[args.step](args)


if __name__ == "__main__":
    main()
