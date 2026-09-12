"""RNN candidate orchestration, entirely synthetic under /tmp/vp_tests/."""
from datetime import datetime
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile

import numpy as np
import pandas as pd
import pytest

from VolumePrediction import refreeze_rnn as rr


NOW = datetime(2026, 9, 9, 10, 3, tzinfo=rr.ET)


@pytest.fixture
def sandbox():
    root = Path("/tmp/vp_tests/refreeze_rnn")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as name:
        repo = Path(name)
        out = repo / "VolumePrediction" / "outputs"
        raw = repo / "price_data" / "volume_prediction" / "raw"
        (out / "panel").mkdir(parents=True)
        (out / "registry").mkdir()
        raw.mkdir(parents=True)
        dates = pd.bdate_range(end="2026-09-04", periods=15)
        ix = pd.MultiIndex.from_product([dates, ["AAA", "BBB"]], names=["date", "ticker"])
        df = pd.DataFrame(index=ix)
        for i, c in enumerate(rr.FEATURE_COLS):
            df[c] = np.linspace(0, 1, len(df)) + i / 100
        df["v"] = 10 + np.linspace(0, 0.1, len(df))
        df["V"] = np.exp(df["v"])
        df["ma5_v"] = 10.0
        df["eta"] = df["v"] - df["ma5_v"]
        df["ret"] = 0.0000123456789012345
        df["fund2_unused"] = 123.0
        panel = out / "panel" / "panel_20260905_120000_prod_20260904.parquet"
        df.to_parquet(panel)
        (out / "panel" / "latest.json").write_text(json.dumps({
            "path": str(panel), "rows": len(df),
            "date_range": [str(dates[0].date()), "2026-09-04"]}))
        pd.DataFrame({"ticker": ["AAA"], "v": [100.0]}).to_parquet(raw / "grouped_2026-09-08.parquet")
        registry = {"models": {}, "production": "old_lgbm", "promotions": [{"keep": True}],
                    "blend": {"enabled": True, "rnn_version": "old_rnn", "rnn_full_coverage": True}}
        (out / "registry" / "registry.json").write_text(json.dumps(registry))
        yield SimpleNamespace(repo=repo, out=out, raw=raw, panel=panel, df=df, registry=registry)


def allow(*_):
    return {"allowed": True, "reasons": []}


def fake_freeze(panel_path, asof, *, art_dir, version, seeds, epochs):
    """Write genuine-shaped numpy artifacts without importing/training torch."""
    df = pd.read_parquet(panel_path)
    assert list(df.columns) == rr.FEATURE_COLS + rr.BASE_COLS
    assert all(df[c].dtype == np.dtype("float32") for c in rr.FEATURE_COLS)
    assert df["ret"].dtype == np.dtype("float64")
    assert (seeds, epochs) == (3, 50)
    tickers = sorted(df.index.get_level_values("ticker").unique())
    art_dir = Path(art_dir)
    per = pd.DataFrame({"active": True}, index=pd.Index(tickers, name="ticker"))
    for c in rr.FEATURE_COLS[:8]:
        per[f"{c}__z_next"] = 0.1
    for c in rr.FEATURE_COLS[8:]:
        per[c] = 0.1
    per["ma5v_next"] = 10.0
    per.to_parquet(art_dir / "per_ticker.parquet")
    np.savez(art_dir / "seq_tail.npz", tickers=np.array(tickers),
             feats=np.zeros((len(tickers), 9, 14), dtype="float32"),
             last_date=np.array([asof] * len(tickers)))
    shapes = {"W_ih": (128, 14), "W_hh": (128, 32), "b_ih": (128,),
              "W2": (16, 32), "b2": (16,), "W3": (8, 16), "b3": (8,),
              "W4": (1, 8), "b4": (1,)}
    for i in range(3):
        np.savez(art_dir / f"weights_s{i}.npz",
                 **{k: np.zeros(v, dtype="float32") for k, v in shapes.items()},
                 seq_len=np.array([10]), n_pred=np.array([14]))
    meta = {"version": version, "kind": "learned.rnn", "trained_through": asof,
            "seq_tail_date": asof, "first_serve_date": "2026-09-08", "seeds": 3,
            "seq_len": 10, "weight_files": [f"weights_s{i}.npz" for i in range(3)],
            "feature_cols": rr.FEATURE_COLS, "n_train_rows": int(df.eta.notna().sum()),
            "fund_cols": rr.FEATURE_COLS[8:],
            "n_tickers": len(tickers), "n_active": len(tickers)}
    (art_dir / "meta.json").write_text(json.dumps(meta))
    return meta


def invoke(sandbox, **kwargs):
    return rr.run(out_dir=sandbox.out, raw_dir=sandbox.raw, repo=sandbox.repo,
                  now=NOW, freeze_fn=fake_freeze, guard_fn=allow, **kwargs)


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_dry_run_footer_only_and_zero_writes(sandbox, monkeypatch):
    before = snapshot(sandbox.repo)
    def forbid(*a, **k):
        raise AssertionError("dry-run must not scan a parquet or train")
    monkeypatch.setattr(pd, "read_parquet", forbid)
    r = invoke(sandbox, dry_run=True)
    assert r["status"] == "dry_run" and r["would_train_now"]
    assert r["asof"] == "2026-09-04" and r["raw_through"] == "2026-09-08"
    assert r["incumbent_version"] == "old_rnn"
    assert snapshot(sandbox.repo) == before


def test_latest_selects_data_end_not_filename_or_future_raw(sandbox):
    future = sandbox.df.copy()
    future.index = pd.MultiIndex.from_product([pd.bdate_range(end="2026-09-09", periods=15),
                                              ["AAA", "BBB"]], names=["date", "ticker"])
    p = sandbox.out / "panel" / "panel_99999999_prod_20260909.parquet"
    future.to_parquet(p)
    pd.DataFrame({"ticker": ["AAA"]}).to_parquet(sandbox.raw / "grouped_2026-09-09.parquet")
    r = invoke(sandbox, dry_run=True)
    assert r["asof"] == "2026-09-04"  # today at 10:03 is not complete
    assert len(r["rejected_panels"]) == 1


def test_explicit_panel_metadata_mismatch_refuses_zero_write(sandbox):
    ptr = sandbox.panel.parent / "latest.json"
    d = json.loads(ptr.read_text())
    d["date_range"][-1] = "2026-09-08"
    ptr.write_text(json.dumps(d))
    before = snapshot(sandbox.repo)
    r = invoke(sandbox, panel_path=sandbox.panel, dry_run=True)
    assert r["exit_code"] == 1 and "disagrees" in r["error"]
    assert snapshot(sandbox.repo) == before


def test_candidate_atomic_publish_and_registry_pointers_unchanged(sandbox):
    r = invoke(sandbox)
    assert r["status"] == "complete" and r["exit_code"] == 0
    art = Path(r["artifact_dir"])
    assert art.exists() and (art / "training_panel.parquet").exists()
    assert not list(art.parent.glob(".*.staging.*"))
    reg = json.loads((sandbox.out / "registry" / "registry.json").read_text())
    for field in ("production", "blend", "promotions"):
        assert reg[field] == sandbox.registry[field]
    model = reg["models"][r["version"]]
    assert model["status"] == "candidate" and model["trained_through"] == "2026-09-04"
    assert model["meta"]["incumbent_version"] == "old_rnn"
    assert model["meta"]["protocol"] == rr.PROTOCOL
    assert json.loads(Path(r["status_file"]).read_text())["status"] == "complete"


def test_idempotent_reuse_preserves_later_metrics_and_shadow_state(sandbox, monkeypatch):
    first = invoke(sandbox)
    reg_path = sandbox.out / "registry" / "registry.json"
    reg = json.loads(reg_path.read_text())
    reg["models"][first["version"]]["metrics"] = {"shadow_days": 5}
    reg_path.write_text(json.dumps(reg))
    art = Path(first["artifact_dir"])
    meta_path = art / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["seq_tail_date"] = "2026-09-09"
    meta_path.write_text(json.dumps(meta))
    before = snapshot(art)
    monkeypatch.setattr(rr, "_narrow_panel", lambda *_: pytest.fail("reused version rescanned source"))
    second = invoke(sandbox)
    assert second["status"] == "reused"
    assert snapshot(art) == before
    assert json.loads(reg_path.read_text())["models"][first["version"]]["metrics"] == {"shadow_days": 5}


def test_training_failure_never_publishes_or_mutates_registry(sandbox):
    before = (sandbox.out / "registry" / "registry.json").read_bytes()
    def fail(*a, **kw):
        (kw["art_dir"] / "weights_s0.npz").write_bytes(b"partial")
        raise RuntimeError("synthetic failure")
    r = rr.run(out_dir=sandbox.out, raw_dir=sandbox.raw, repo=sandbox.repo,
               now=NOW, freeze_fn=fail, guard_fn=allow)
    assert r["exit_code"] == 1 and not Path(r["artifact_dir"]).exists()
    assert (sandbox.out / "registry" / "registry.json").read_bytes() == before
    assert not list((sandbox.out / "registry" / "artifacts").glob(".*.staging.*"))


def test_invalid_weights_refuse_publish(sandbox):
    def broken(*a, **k):
        m = fake_freeze(*a, **k)
        (k["art_dir"] / "weights_s2.npz").write_bytes(b"broken")
        return m
    r = rr.run(out_dir=sandbox.out, raw_dir=sandbox.raw, repo=sandbox.repo,
               now=NOW, freeze_fn=broken, guard_fn=allow)
    assert r["exit_code"] == 1 and not Path(r["artifact_dir"]).exists()


def test_existing_partial_candidate_is_not_overwritten(sandbox):
    art = sandbox.out / "registry" / "artifacts" / "rnn_prod_20260904"
    art.mkdir(parents=True)
    (art / "incomplete").write_text("preserve")
    r = invoke(sandbox)
    assert r["exit_code"] == 1
    assert (art / "incomplete").read_text() == "preserve"
    assert len(list(art.iterdir())) == 1


def test_guard_refusal_exit3_and_no_training(sandbox):
    def blocked(*_):
        return {"allowed": False, "reasons": ["pairs_running"]}
    r = rr.run(out_dir=sandbox.out, raw_dir=sandbox.raw, repo=sandbox.repo,
               now=NOW, guard_fn=blocked, freeze_fn=lambda *a, **k: pytest.fail("trained"))
    assert r["status"] == "deferred" and r["exit_code"] == 3
    assert not (sandbox.out / "registry" / "artifacts").exists()


def test_pairs_start_and_live_pid_guard(sandbox, monkeypatch):
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="123 123 bash conductor/pipeline_runner.sh\n"))
    state = sandbox.repo / "pipeline_state"
    (state / "logs").mkdir(parents=True)
    (state / "runner.pid").write_text("123")
    (state / "logs" / "pipeline_current.log").write_text("PIPELINE COMPLETE\nPIPELINE START\n")
    g = rr.guard_status(sandbox.repo, NOW)
    assert not g["allowed"]
    assert set(g["reasons"]) >= {"pairs_live_runner.pid", "pairs_process_running", "pairs_unmatched_PIPELINE_START"}


@pytest.mark.parametrize("hour,minute,allowed", [(9, 29, False), (9, 30, True),
                                                (14, 30, True), (14, 31, False), (20, 30, False)])
def test_daytime_start_window(sandbox, monkeypatch, hour, minute, allowed):
    monkeypatch.setattr(rr.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=""))
    assert rr.guard_status(sandbox.repo, NOW.replace(hour=hour, minute=minute))["allowed"] is allowed


def test_concurrent_lock_refuses_second_trainer(sandbox):
    d = sandbox.out / "refreeze_rnn"
    d.mkdir()
    with (d / "training.lock").open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        (d / "status.json").write_text('{"status":"training","pid":123}')
        r = invoke(sandbox)
    assert r["exit_code"] == 3 and "another" in r["reason"]
    assert json.loads((d / "status.json").read_text()) == {"status": "training", "pid": 123}


def test_damaged_registry_fails_closed(sandbox):
    p = sandbox.out / "registry" / "registry.json"
    p.write_text("broken registry")
    r = invoke(sandbox)
    assert r["exit_code"] == 1 and p.read_text() == "broken registry"


def test_existing_complete_candidate_can_reuse_outside_training_window(sandbox):
    invoke(sandbox)
    r = rr.run(out_dir=sandbox.out, raw_dir=sandbox.raw, repo=sandbox.repo,
               now=NOW.replace(hour=2), guard_fn=lambda *_: {"allowed": False, "reasons": ["night"]},
               freeze_fn=lambda *a, **k: pytest.fail("reused candidate retrained"))
    assert r["status"] == "reused" and r["exit_code"] == 0


def test_nonfinite_target_prevents_training(sandbox):
    df = sandbox.df.copy()
    df.iloc[0, df.columns.get_loc("eta")] = float("inf")
    df.to_parquet(sandbox.panel)
    r = invoke(sandbox)
    assert r["exit_code"] == 1 and "nonfinite base/target" in r["error"]
    assert not Path(r["artifact_dir"]).exists()


def test_unusable_frozen_features_never_publish(sandbox):
    def broken(*a, **k):
        meta = fake_freeze(*a, **k)
        p = k["art_dir"] / "per_ticker.parquet"
        per = pd.read_parquet(p).drop(columns=["ma5v_next"])
        per.to_parquet(p)
        return meta
    r = rr.run(out_dir=sandbox.out, raw_dir=sandbox.raw, repo=sandbox.repo,
               now=NOW, freeze_fn=broken, guard_fn=allow)
    assert r["exit_code"] == 1 and "frozen serving features" in r["error"]
    assert not Path(r["artifact_dir"]).exists()
