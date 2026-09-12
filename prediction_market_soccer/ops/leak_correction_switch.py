"""ops/leak_correction_switch.py — the production switch for the owner-authorised leak correction.

One explicit, re-runnable command per step, each with its own acceptance check; it stops at
the first failed check. Rehearsed end to end on an isolated clone on 2026-09-11 (switch and
rollback). See LEAK_CORRECTION_RUNBOOK_20260911.md.

    python -m prediction_market_soccer.ops.leak_correction_switch precheck
    python -m prediction_market_soccer.ops.leak_correction_switch rebuild   # from a fresh snapshot
    python -m prediction_market_soccer.ops.leak_correction_switch bundle
    python -m prediction_market_soccer.ops.leak_correction_switch register
    python -m prediction_market_soccer.ops.leak_correction_switch switch
    python -m prediction_market_soccer.ops.leak_correction_switch verify
    python -m prediction_market_soccer.ops.leak_correction_switch rollback

Layout (all under the repo root, which is the isolated root for the switch):
    <repo>/.soccer-isolated                                   'soccer-isolated-v1'
    <repo>/prediction_market_soccer/data/book_candidates/leakfix-20260911/
        candidate/prices.db   sealed corrected-price candidate (read-only)
        source_snapshot.db    the read-only snapshot the candidate was collected from
        manifest.json         the collection manifest (run_id, scope)
        snapshot_<ts>.db      fresh production snapshot taken at 'rebuild' time
        out/rebuild.json      the corrected records built from that snapshot
        bundle/               the five certified documents
        recovery/             version_workflow journal for the switch
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

REPO = pathlib.Path(__file__).resolve().parents[2]
MOD = REPO / "prediction_market_soccer"
DUR = MOD / "data" / "book_candidates" / "leakfix-20260911"
FRONTEND = REPO / "someo-park-investment-management" / "public" / "data" / "soccer"   # the served soccer dir; public/data is the World Cup module
AUTHORIZATION = {
    "owner": "repository owner (chat instruction, 2026-09-11)",
    "authorized_at": "2026-09-11T17:00:00+00:00",
    "statement": "我允许解除冻结 按照最正确的方式修复 重跑然后 再冻结成新的冻结账本 只要你按照正确方式做出来",
    "operator": "Claude Code session 013NcYaAaDYpPCUDSmBqNWmT",
    "channel": "chat instruction",
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _conn():
    from prediction_market_soccer.ingest import store
    conn = store.init_db()
    main = next(r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main")
    assert main == str(MOD / "data" / "soccer.db"), main
    return conn


def _manifest():
    return json.loads((DUR / "manifest.json").read_text())


def precheck() -> None:
    from prediction_market_soccer.util.frozen_strategy_store import _require_no_open_positions, active_version, read_book
    from prediction_market_soccer.ops.version_workflow import _pending_completion
    conn = _conn()
    live = conn.execute("SELECT COUNT(*) FROM fixture WHERE status_short IN ('1H','2H','HT') AND league_id IN "
                        "(39,140,135,78,61,2,3,848,13,11,71,128)").fetchone()[0]
    print("进行中比赛:", live)
    _require_no_open_positions(conn); print("未平仓位: 无 ✓")
    _pending_completion(conn); print("未追加 completion: 无 ✓")
    from prediction_market_soccer.ops.leak_corrected_book import unsealed_paper_entries
    print("未封口 paper 入场:", unsealed_paper_entries(conn), "(必须为 0)")
    v = active_version(conn); b = read_book(conn)
    print("激活版本:", v["version_id"][:12], v["origin"], "| 记录", len(b["ledger"]["records"]),
          "| 末行累计", {k: b["ledger"]["records"][-1][k] for k in ("pre_cum_pnl_cents", "inplay_cum_pnl_cents", "combined_cum_pnl_cents")})
    print("隔离根标记:", (REPO / ".soccer-isolated").read_text().strip() if (REPO / ".soccer-isolated").exists() else "缺失(switch 前写入)")
    for p in (DUR / "candidate" / "prices.db", DUR / "source_snapshot.db", DUR / "manifest.json"):
        print("  持久文件:", p.name, "✓" if p.exists() else "缺失")
    for proc in ("live_refresh", "refresh_all", "settle_reports", "proc_lock", "match_trigger"):
        import subprocess
        pat = f"prediction_market_soccer.ops.{proc}" if proc != "proc_lock" else "proc_lock --run"
        r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
        print(f"  进程 {proc}:", "运行中 ✗" if r.stdout.strip() else "无 ✓")
    # The epoch pin is only verified by version_workflow AFTER the new book has already
    # been committed and the six artifacts promoted; check it here, before anything moves,
    # so a drifted runtime aborts while the system is still untouched.
    from prediction_market_soccer.util import forward_methods as FM
    ep = FM.active_epoch(conn)
    if not ep:
        sys.exit("无激活 epoch,切换后无法重绑")
    if ep["manifest"]["runtime"] != FM.runtime_snapshot():
        sys.exit(f"epoch {ep['epoch_id'][:12]} 的运行时指纹与当前代码不符 —— 先修复再切换")
    print("epoch 运行时指纹:", ep["epoch_id"][:12], "一致 ✓ | 绑定", ep["book_version_id"][:12])
    # rebuild snapshots the production DB and switch stages two artifact dirs plus a
    # pre-change DB backup; size the requirement off the live DB rather than a constant.
    import shutil
    need = (MOD / "data" / "soccer.db").stat().st_size * 3
    free = shutil.disk_usage(MOD / "data").free
    print(f"磁盘: 需 ~{need/2**30:.1f}G,可用 {free/2**30:.1f}G", "✓" if free > need * 1.5 else "✗ 不足")
    if free <= need * 1.5:
        sys.exit("磁盘余量不足以安全完成切换")
    stop = MOD / "data" / ".soccer-maintenance.json"
    if stop.exists():
        print("⚠ 残留维护标记:", stop.read_text().strip(), "→ switch 会复用该 operation_id")
    if live:
        sys.exit("有比赛进行中,窗口未到")


def rebuild() -> None:
    """Fresh production snapshot → corrected records (the book may have grown since the rehearsal)."""
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.util.frozen_strategy_store import read_book
    from prediction_market_soccer.ops import leak_corrected_book as L
    from prediction_market_soccer.ops.version_workflow import _pending_completion
    from prediction_market_soccer.ops import performance_report as P
    # Seal and append every completed paper leg FIRST, then snapshot — otherwise the
    # rebuild input is stale the moment consume_completed_paper runs (review 2026-09-11).
    live = _conn()
    P.build(live, freeze=True)
    _pending_completion(live)
    assert L.unsealed_paper_entries(live) == 0, "unsealed paper entries; wait"
    live.close()
    snap = DUR / f"snapshot_{_now()}.db"
    src = sqlite3.connect(str(MOD / "data" / "soccer.db"))
    dst = sqlite3.connect(str(snap))
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    print("快照:", snap.name, f"{snap.stat().st_size/1e6:.0f} MB")
    store.DB_PATH = snap
    conn = store.init_db()
    book = read_book(conn)
    quotes = L.load_candidate_quotes(DUR / "candidate" / "prices.db", _manifest()["run_id"])
    recs, summary = L.rebuild(conn, book["ledger"]["records"], quotes, run_id=_manifest()["run_id"])
    (DUR / "out").mkdir(exist_ok=True)
    (DUR / "out" / "rebuild.json").write_text(json.dumps({"summary": summary, "records": recs, "snapshot": snap.name,
                                                          "previous_version_id": book["version"]["version_id"]}, ensure_ascii=False))
    print(json.dumps({k: v for k, v in summary.items() if k != "unresolved_fixtures"}, ensure_ascii=False))
    print("unresolved:", summary["unresolved_fixtures"])
    assert summary["unresolved_fixtures"] == [1607650], "unexpected unresolved set"
    assert summary["legacy"] + summary["forward"] == len(book["ledger"]["records"])
    assert summary["live_fidelity"]["exit_same"] >= 89
    print("重建通过 ✓  记录", len(recs))


def bundle() -> None:
    from prediction_market_soccer.util.frozen_strategy_store import read_book
    from prediction_market_soccer.ops import leak_corrected_book as L
    conn = _conn()
    prev = read_book(conn)
    rb = json.loads((DUR / "out" / "rebuild.json").read_text())
    assert rb["previous_version_id"] == prev["version"]["version_id"], "book changed since rebuild; run rebuild again"
    assert len(rb["records"]) == len(prev["ledger"]["records"]), "record count drifted; run rebuild again"
    m = _manifest()
    report = L.assemble_report(prev, rb["records"], run_id=m["run_id"], summary=rb["summary"])
    ci = L.candidate_input_for(REPO, DUR / "candidate" / "prices.db", DUR / "source_snapshot.db", m["run_id"], m["collection_scope"]["scope_id"])
    if (DUR / "bundle").exists():
        shutil.move(str(DUR / "bundle"), str(DUR / f"bundle_superseded_{_now()}"))
    out = L.make_bundle(root=REPO, output_dir=DUR / "bundle", report=report, prev_book=prev, summary=rb["summary"],
                        candidate_input=ci, run_id=m["run_id"], authorization=AUTHORIZATION, attempt=_now())
    from prediction_market_soccer.ops.version_workflow import validate_backtest_bundle
    v = validate_backtest_bundle(DUR / "bundle", root=REPO)
    print("bundle ✓", out["directory"], "| ledger", out["manifest"]["ledger_id"][:12], "| docs", sorted(v))


def register() -> None:
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    from prediction_market_soccer.ops import leak_corrected_book as L
    conn = _conn()
    before = active_version(conn)["version_id"]
    vid = L.register(conn, root=REPO, bundle_dir=DUR / "bundle")
    after = active_version(conn)["version_id"]
    assert after == before, "registration must not activate"
    rows = conn.execute("SELECT substr(version_id,1,12), origin, method_version FROM strategy_book_version").fetchall()
    print("已注册:", vid[:12], "| 激活仍为", after[:12], "| 版本表:", [tuple(r) for r in rows])
    (DUR / "registered_version_id.txt").write_text(vid)


def switch() -> None:
    from prediction_market_soccer.ops import leak_corrected_book as L
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    conn = _conn()
    (REPO / ".soccer-isolated").write_text("soccer-isolated-v1")
    vid = (DUR / "registered_version_id.txt").read_text().strip()
    m = _manifest()
    ci = L.candidate_input_for(REPO, DUR / "candidate" / "prices.db", DUR / "source_snapshot.db", m["run_id"], m["collection_scope"]["scope_id"])
    dirs = [MOD / "data" / "output", FRONTEND]
    # maintenance_gate fsyncs {"operation_id": ...} before it takes the exclusive flock, so an
    # interrupted attempt leaves the one id the gate will re-admit. Minting a fresh id here would
    # make the runbook's documented "retry with the same operation_id" unreachable from the CLI.
    stop = MOD / "data" / ".soccer-maintenance.json"
    operation_id = f"leakfix-prod-{_now()}"
    if stop.exists():
        prior = json.loads(stop.read_text()).get("operation_id")
        if prior:
            operation_id = prior
            print("复用残留 operation_id:", operation_id)
    head = L.switch_book(conn, root=REPO, directories=dirs, recovery_dir=DUR / "recovery", operation_id=operation_id,
                         version_id=vid, bundle_dir=DUR / "bundle", candidate_input=ci)
    from prediction_market_soccer.util import forward_methods as FM
    ep = FM.active_epoch(conn)
    print("switch ✓ 激活:", active_version(conn)["version_id"][:12], "| head records", len(head["fixture_ids"]),
          "| epoch", ep["epoch_id"][:12], "绑定", ep["book_version_id"][:12])


def verify() -> None:
    import hashlib
    from prediction_market_soccer.util.frozen_strategy_store import active_version, read_book
    conn = _conn()
    v = active_version(conn); b = read_book(conn)
    print("激活:", v["version_id"][:12], v["origin"], v["method_version"])
    print("末行累计:", {k: b["ledger"]["records"][-1][k] for k in ("pre_cum_pnl_cents", "inplay_cum_pnl_cents", "combined_cum_pnl_cents")})
    print("激活表:", [tuple(r) for r in conn.execute("SELECT sequence, substr(version_id,1,8), operation FROM strategy_book_activation")])
    j = json.loads((DUR / "recovery" / "journal.json").read_text()); print("journal:", j["phase"])
    from prediction_market_soccer.util import forward_methods as FM
    ep = FM.active_epoch(conn); print("epoch 绑定:", ep["book_version_id"][:12], "== 激活账本" if ep["book_version_id"] == v["version_id"] else "✗ 未重绑")
    from prediction_market_soccer.ops import paper_trading
    try:
        st = paper_trading.run_cycle(conn, {"matches": []}); print("paper run_cycle:", st.get("state"), st.get("errors"))
    except Exception as exc:  # noqa: BLE001
        print("paper run_cycle 异常:", str(exc)[:160])
    for name in ("performance_report.json", "milestone_marks.json", "performance_report.pdf"):
        a = hashlib.sha256((MOD / "data" / "output" / name).read_bytes()).hexdigest()[:12]
        c = hashlib.sha256((FRONTEND / name).read_bytes()).hexdigest()[:12]
        print(f"  {name}: output {a} frontend {c} {'同' if a == c else '异 ✗'}")
    rep = json.loads((MOD / "data" / "output" / "performance_report.json").read_text())
    lc = rep["evidence_summary"].get("leak_correction") or {}
    print("report combined:", rep["combined_pnl_cents_total"], "| owner_authorized", lc.get("owner_authorized"), "| strict_pit", lc.get("strict_pit_certified"))
    mk = json.loads((MOD / "data" / "output" / "milestone_marks.json").read_text())
    mm = next((x for x in mk["matches"] if x["fixture_id"] == 1623407), None)
    t60 = next((k for k in (mm or {}).get("marks", []) if k.get("milestone") == "T60"), None)
    print("1623407 T60:", t60 and (t60.get("score"), t60.get("poly_c"), t60.get("state_status")))


def rollback() -> None:
    from prediction_market_soccer.ops import version_workflow as VW, milestone_export as M
    from prediction_market_soccer.ops.performance_report import report_from_book, build_pdf, validate_strategy_views
    from prediction_market_soccer.ops.run_status import atomic_bytes
    from prediction_market_soccer.util.frozen_strategy_store import active_version
    from dataclasses import asdict
    conn = _conn()
    prev_id = conn.execute("SELECT previous_version_id FROM strategy_book_activation ORDER BY sequence DESC LIMIT 1").fetchone()[0]
    print("恢复到:", prev_id[:12])

    def render(book, dirs):
        rep = report_from_book(book); marks = M.marks_for_book(conn, rep.strategy_ledger, book["version"])
        first = pathlib.Path(dirs[0])
        atomic_bytes(first / "performance_report.json", json.dumps(asdict(rep), ensure_ascii=False, allow_nan=False).encode())
        atomic_bytes(first / "milestone_marks.json", json.dumps(marks, ensure_ascii=False, allow_nan=False).encode())
        build_pdf(rep, str(first / "performance_report.pdf")); validate_strategy_views(first, rep.strategy_ledger)
        for d in dirs[1:]:
            for n in ("performance_report.json", "milestone_marks.json", "performance_report.pdf"):
                atomic_bytes(pathlib.Path(d) / n, (first / n).read_bytes())
            validate_strategy_views(pathlib.Path(d), rep.strategy_ledger)
    dirs = [MOD / "data" / "output", FRONTEND]
    ts = _now()
    VW.switch(conn, root=REPO, directories=dirs, recovery_dir=DUR / f"recovery_rollback_{ts}", operation_id=f"leakfix-rollback-{ts}",
              expected_head=VW.head(conn, dirs), version_id=prev_id, render=render, restoring=True)
    from prediction_market_soccer.ops.leak_corrected_book import rebind_epoch_after_restore
    rebind_epoch_after_restore(conn, root=REPO, directories=dirs, recovery_dir=DUR / f"recovery_rollback_{ts}_epoch", operation_id=f"leakfix-rollback-{ts}-epoch")
    print("回滚 ✓ 激活:", active_version(conn)["version_id"][:12], "(epoch 已重绑)")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "precheck"
    {"precheck": precheck, "rebuild": rebuild, "bundle": bundle, "register": register,
     "switch": switch, "verify": verify, "rollback": rollback}[step]()
