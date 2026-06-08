"""
RunDailySignalSandbox.py — run a daily signal FULLY ISOLATED for testing.

Reads the REAL inputs (prices, walk-forward outputs, inventory, calendars) but
redirects EVERY output to a throwaway sandbox dir. Zero production impact:
  - dry_run=True  → skips inventory save, web sync, perf-update subprocess,
                    earnings-cache update (all already guarded by `not dry_run`).
  - SIGNALS_DIR / INVENTORY_HISTORY_DIR / event-veto-state / heartbeat → sandbox.
So it does NOT replace inventory_*.json, does NOT write trading_signals/, does NOT
touch the web dashboard, does NOT rewrite master/strategy performance.

Usage (use the matching conda env):
  conda run -n someopark_run python RunDailySignalSandbox.py mtfs 2026-06-04
  conda run -n someopark_run python RunDailySignalSandbox.py mrpt 2026-06-04
  conda run -n qlib_run     python RunDailySignalSandbox.py aiss 2026-06-04

The sandbox path is printed; inspect its files, then delete it. Nothing else changes.
"""
import os
import sys
import json
import shutil
import tempfile
import datetime as dt
import pathlib

_ROOT = os.path.dirname(os.path.abspath(__file__))


def _sandbox() -> str:
    d = tempfile.mkdtemp(prefix="daily_signal_sandbox_")
    return d


def _redirect_pairs_outputs(DS, sandbox: str):
    # Redirect ALL output paths to the sandbox (inputs stay real).
    # NOTE: TRADING_DIR / SIGNALS_DIR / REPORTS_DIR are SEPARATE module bindings
    # (all initialised to trading_signals) — reassign EACH, not just one.
    DS.SIGNALS_DIR = sandbox
    DS.TRADING_DIR = sandbox
    DS.REPORTS_DIR = sandbox
    DS.INVENTORY_HISTORY_DIR = sandbox
    DS._EVENT_STATE_DIR = sandbox          # per-strategy veto state -> sandbox
    DS._EVENT_HEARTBEAT = os.path.join(sandbox, "event_risk_heartbeat.log")


def run_pairs(strategy: str, signal_date: dt.date, sandbox: str):
    """MRPT / MTFS — root DailySignal (someopark_run), dry_run (no inventory write)."""
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    import DailySignal as DS
    _redirect_pairs_outputs(DS, sandbox)
    return DS.run_daily_signal(strategy=strategy, signal_date=signal_date, dry_run=True)


def run_pairs_execute(strategy: str, signal_date: dt.date, sandbox: str):
    """MRPT / MTFS EXECUTE mode (dry_run=False) — the inventory IS updated, but the
    inventory path is redirected to a SANDBOX COPY so production inventory is never
    opened for write. Also neutralises the web-sync (shutil.copy2 outside sandbox) and
    the perf-update subprocesses (UpdateBDC/Master/Strategy). Use to inspect the REAL
    post-reduce inventory file with zero production impact."""
    import shutil as _sh
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    import DailySignal as DS
    _redirect_pairs_outputs(DS, sandbox)

    # Inventory read+write -> sandbox copies (production inventory never written).
    for strat in ("mrpt", "mtfs"):
        src = os.path.join(_ROOT, f"inventory_{strat}.json")
        if os.path.exists(src):
            _sh.copy2(src, os.path.join(sandbox, f"inventory_{strat}.json"))
    DS.inventory_path = lambda s: os.path.join(sandbox, f"inventory_{s}.json")

    # Neutralise production side effects of a non-dry run:
    # web-sync uses shutil.copy2 → only allow sandbox dests (patch the global module,
    # which DailySignal's `import shutil` shares).
    import shutil as _shmod, subprocess as _spmod
    _orig_copy2 = _shmod.copy2
    def _guarded_copy2(srcp, dstp, *a, **k):
        if str(dstp).startswith(sandbox):
            return _orig_copy2(srcp, dstp, *a, **k)
        return None                              # skip any copy that would touch production
    _shmod.copy2 = _guarded_copy2
    _spmod.run = lambda *a, **k: None            # skip perf-update subprocesses (UpdateBDC/Master)
    try:                                          # skip risk-workbook/PDF (writes outside sandbox)
        import RiskManager as _rm
        _rm.generate_risk_report = lambda *a, **k: None
    except Exception:
        pass

    return DS.run_daily_signal(strategy=strategy, signal_date=signal_date, dry_run=False)


def run_aiss(signal_date: dt.date, sandbox: str):
    """AISS — qlib-main AISSdailySignal (qlib_run)."""
    qlib = os.path.join(_ROOT, "qlib-main")
    if qlib not in sys.path:
        sys.path.insert(0, qlib)
    from semiconductor_strategy import AISSdailySignal as A
    A.SIGNALS_DIR = pathlib.Path(sandbox)         # reports + heartbeat
    rep = A.run_daily_signal(signal_date=signal_date, dry_run=True)
    return rep


def _open_pairs(inv_path):
    """Set of pairs with an open direction in an inventory file."""
    if not os.path.exists(inv_path):
        return set()
    inv = json.load(open(inv_path))
    return {k for k, v in inv.get("pairs", {}).items()
            if isinstance(v, dict) and v.get("direction")}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    execute = "--execute" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)
    strategy = args[0].lower()
    sd = dt.date.fromisoformat(args[1]) if len(args) > 1 else dt.date.today()
    sandbox = _sandbox()
    mode = "EXECUTE (non-dry, inventory→sandbox)" if execute else "dry-run"
    print(f"=== SANDBOX daily-signal test [{mode}]: strategy={strategy} date={sd} ===")
    print(f"    sandbox (all outputs here, delete after): {sandbox}")

    before = {s: _open_pairs(os.path.join(_ROOT, f"inventory_{s}.json"))
              for s in ("mrpt", "mtfs")} if execute else {}
    try:
        if strategy in ("mtfs", "mrpt", "both"):
            rep = (run_pairs_execute if execute else run_pairs)(strategy, sd, sandbox)
        elif strategy == "aiss":
            if execute:
                print("execute mode is pairs-only (AISS de-risk is weight-based; "
                      "use dry-run + read risk_flags). Running dry-run.");
            rep = run_aiss(sd, sandbox)
        else:
            print(f"unknown strategy: {strategy}"); sys.exit(1)
    except Exception as e:  # noqa: BLE001
        import traceback; traceback.print_exc()
        print(f"\n[SANDBOX] run failed: {e!r}")
        sys.exit(2)

    print("\n=== sandbox outputs ===")
    for f in sorted(os.listdir(sandbox)):
        print("   ", f)
    hb = os.path.join(sandbox, "event_risk_heartbeat.log")
    if os.path.exists(hb):
        print("\n=== event-risk heartbeat (sandbox) ===")
        print(open(hb).read().strip())

    if execute:
        print("\n=== inventory CHANGE (sandbox copy; production NOT written) ===")
        for s in ("mrpt", "mtfs"):
            after = _open_pairs(os.path.join(sandbox, f"inventory_{s}.json"))
            closed = sorted(before.get(s, set()) - after)
            if before.get(s) or after:
                print(f"  [{s}] open {len(before.get(s, set()))} -> {len(after)}"
                      f"   CLOSED by run: {closed if closed else '(none)'}")
    print(f"\n[SANDBOX] done. Production untouched. Remove with:  rm -rf {sandbox}")


if __name__ == "__main__":
    main()
