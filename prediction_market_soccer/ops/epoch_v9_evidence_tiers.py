"""Forward-method epoch v9: evidence tiers recomputed at read time (2026-09-12).

ops/performance_report.report_from_book inherited the frozen metadata evidence tier
array verbatim, so the corrected book served the old book.s posthoc tier counts and
the old book froze its forward tier at zeros. Tiers are now derived per read from
the records (semantics reproduce the v7-era frozen array bit-for-bit); method rules
are unchanged, so v8 stays in compatible_epoch_ids and forward entries keep feeding
PIT calibration.

Steps: precheck | activate | verify — run with services unloaded.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
MOD = REPO / "prediction_market_soccer"
FRONTEND = REPO / "someo-park-investment-management" / "public" / "data" / "soccer"
DUR = MOD / "data" / "book_candidates" / "epoch_v9_20260912"
V8_EPOCH = "66b4b708c658"
BOOK = "58fbc07692b9"
METHOD_V9 = "soccer-timing-integrity-20260912-v9-evidence-tiers"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _conn():
    from prediction_market_soccer.ingest import store
    return store.init_db()


def precheck() -> None:
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.util.frozen_strategy_store import _require_no_open_positions, active_version
    from prediction_market_soccer.ops.version_workflow import _pending_completion
    conn = _conn()
    live = conn.execute("SELECT COUNT(*) FROM fixture WHERE status_short IN ('1H','2H','HT','ET','P','BT')").fetchone()[0]
    assert live == 0, f"{live} matches in progress"
    _require_no_open_positions(conn); print("未平仓位: 无 ✓")
    _pending_completion(conn); print("未追加 completion: 无 ✓")
    v = active_version(conn)
    assert v["version_id"].startswith(BOOK), v["version_id"]
    print("激活账本:", v["version_id"][:12], "✓")
    ep = FM.active_epoch(conn)
    assert ep["epoch_id"].startswith(V8_EPOCH), ep["epoch_id"]
    runtime_changed = ep["manifest"]["runtime"] != FM.runtime_snapshot()
    print("v8 激活中,runtime 已漂移(=修复已落盘):", runtime_changed)
    assert runtime_changed, "代码没改?v9 无从谈起"
    marker = REPO / ".soccer-isolated"
    assert marker.exists() and marker.read_text().strip() == "soccer-isolated-v1", "隔离标记缺失"
    import subprocess
    for proc in ("live_refresh", "refresh_all", "settle_reports"):
        r = subprocess.run(["pgrep", "-f", f"prediction_market_soccer.ops.{proc}"], capture_output=True, text=True)
        assert not r.stdout.strip(), f"{proc} 仍在跑"
    print("precheck ✓")


def activate() -> None:
    from prediction_market_soccer.util import forward_methods as FM
    from prediction_market_soccer.ops import version_workflow as VW
    conn = _conn()
    v8 = FM.active_epoch(conn)
    assert v8["epoch_id"].startswith(V8_EPOCH)
    # paper_trading reads only the ACTIVE epoch's compatible set (no transitive chase),
    # so v9 must carry v8 AND everything v8 already vouched for (v7) or those forward
    # entries drop out of PIT calibration.
    manifest = FM.build_manifest(model_version=v8["model_version"], method_version=METHOD_V9,
                                 compatible_epoch_ids=[v8["epoch_id"], *v8["manifest"]["compatible_epoch_ids"]])
    assert manifest["rules"] == v8["manifest"]["rules"], "规则必须不变(这是序列化修复,不是方法变更)"
    DUR.mkdir(parents=True, exist_ok=True)
    (DUR / "manifest_v9.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    dirs = [MOD / "data" / "output", FRONTEND]
    # activate_forward_method 自建 recovery(exist_ok=False),不得预创建;
    # 残留门禁标记时复用其 operation_id(门禁可重入,见 maintenance_gate)。
    stop = MOD / "data" / ".soccer-maintenance.json"
    operation_id = f"epoch-v9-evidence-tiers-{_now()}"
    if stop.exists():
        prior = json.loads(stop.read_text()).get("operation_id")
        if prior:
            operation_id = prior
            print("复用残留 operation_id:", operation_id)
    VW.activate_forward_method(conn, root=REPO, directories=dirs, recovery_dir=DUR / "recovery",
                               operation_id=operation_id,
                               expected_head=VW.head(conn, dirs), manifest=manifest)
    ep = FM.active_epoch(conn)
    print("activate ✓ epoch", ep["epoch_id"][:12], "|", ep["method_version"], "| 绑定", ep["book_version_id"][:12])


def verify() -> None:
    from prediction_market_soccer.util import forward_methods as FM
    conn = _conn()
    ep = FM.active_epoch(conn)
    assert ep["method_version"] == METHOD_V9, ep["method_version"]
    assert ep["book_version_id"].startswith(BOOK)
    assert ep["manifest"]["runtime"] == FM.runtime_snapshot(), "v8 指纹与当前代码不一致"
    assert any(c.startswith(V8_EPOCH) for c in ep["manifest"]["compatible_epoch_ids"]), "v8 必须在兼容集"
    print("epoch:", ep["epoch_id"][:12], METHOD_V9, "| runtime 一致 ✓ | v8 兼容 ✓ | 绑定", ep["book_version_id"][:12])
    from prediction_market_soccer.ops import paper_trading
    out = paper_trading.run_cycle(conn)
    assert out.get("state") in ("ok", "idle", None) and not out.get("errors"), out
    print("paper run_cycle:", out.get("state", "idle"), out.get("errors", []))
    from prediction_market_soccer.exec import kalshi_mirror as km
    res = km.run_cycle(conn)
    assert not res.get("errors"), res
    print("kalshi mirror run_cycle:", res.get("summary", res))
    print("verify ✓")


if __name__ == "__main__":
    {"precheck": precheck, "activate": activate, "verify": verify}[sys.argv[1]]()
