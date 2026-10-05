"""w7_scenarios.inputs._cached (2026-10-05): concurrent writers never share a temp file."""
import json

import pandas as pd

from crypto_trading.crypto_strategies.w7_scenarios import inputs


def test_cache_writes_use_private_temp_names_and_leave_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(inputs, "CACHE", tmp_path)
    src = tmp_path / "src.jsonl"; src.write_text("x\n")
    monkeypatch.setattr(inputs, "_closed", lambda s: True)
    d = tmp_path / "strips" / "BTC"; d.mkdir(parents=True)
    (d / "2026-10-01.tmp").write_text("another writer's legacy temp file")   # must not be touched
    df = inputs._cached("strips", "BTC", "2026-10-01", [src], lambda: pd.DataFrame({"a": [1, 2]}))
    assert list(df["a"]) == [1, 2]
    assert (d / "2026-10-01.parquet").exists() and json.loads((d / "2026-10-01.json").read_text())["version"] == inputs.VERSION
    assert (d / "2026-10-01.tmp").read_text() == "another writer's legacy temp file"
    assert sorted(p.name for p in d.iterdir()) == ["2026-10-01.json", "2026-10-01.parquet", "2026-10-01.tmp"]
    # a second (cached) read rebuilds nothing
    calls = []
    out = inputs._cached("strips", "BTC", "2026-10-01", [src], lambda: calls.append(1) or pd.DataFrame())
    assert calls == [] and list(out["a"]) == [1, 2]
