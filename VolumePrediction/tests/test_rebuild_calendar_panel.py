"""Synthetic streaming, preservation and fail-closed publication checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from VolumePrediction import rebuild_calendar_panel as repair
from VolumePrediction.features import pipeline


@pytest.fixture
def source(tmp_path):
    dates = pd.to_datetime(["2026-06-18", "2026-06-26", "2026-09-03", "2026-09-04"])
    index = pd.MultiIndex.from_product([dates, ["AAA", "ZZZ"]], names=["date", "ticker"])
    df = pd.DataFrame({"V": np.arange(8, dtype=float) + 10,
                       "v": np.log(np.arange(8, dtype=float) + 10),
                       "eta": [np.nan, 0.1, np.nan, 0.2, 0.3, 0.4, 0.5, np.nan],
                       "ma5_v": np.arange(8, dtype=float),
                       "tech_float32": np.arange(8, dtype=np.float32),
                       "earn_zero": [0., 1., 0., 0., 1., 0., 0., 0.],
                       "fund_label": ["x", None, "中", "", "x", "y", "z", "q"]}, index=index)
    for col in repair.CALENDAR_COLUMNS:
        df[col] = 0.0
    # Reproduce the actual final-day pollution, plus deliberately dirty other dates.
    df.loc[(pd.Timestamp("2026-09-04"), slice(None)), "cal_triple_witching"] = 1.0
    df.loc[(pd.Timestamp("2026-09-04"), slice(None)), "cal_double_witching"] = 1.0
    table = pa.Table.from_pandas(df)
    col = table.schema.get_field_index("tech_float32")
    values = pa.array([0., -0., float("nan"), None, 1., 2., 3., 4.],
                      type=pa.float32(), from_pandas=False)
    table = table.set_column(col, table.schema.field(col), values)
    metadata = dict(table.schema.metadata)
    metadata[b"custom_provenance"] = b"keep unchanged"
    table = table.replace_schema_metadata(metadata)
    path = tmp_path / "source.parquet"
    pq.write_table(table, path, row_group_size=3)
    return path


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_streaming_rebuild_preserves_all_other_data_and_audits_every_change(source):
    original_hash = _digest(source)
    output = source.with_name("rebuilt.parquet")
    result = repair.rebuild(source, output, batch_size=2)
    original = pq.read_table(source)
    rebuilt = pq.read_table(output)
    assert original.schema.equals(rebuilt.schema, check_metadata=True)
    assert rebuilt.num_rows == original.num_rows == 8
    for col in original.column_names:
        if col not in repair.CALENDAR_COLUMNS:
            assert repair._exact_array_equal(original[col].combine_chunks(), rebuilt[col].combine_chunks())
    before = original.to_pandas()
    after = rebuilt.to_pandas()
    expected = pipeline.add_calendar_flags(before)
    pd.testing.assert_frame_equal(after[list(repair.CALENDAR_COLUMNS)],
                                  expected[list(repair.CALENDAR_COLUMNS)])
    changed = before[list(repair.CALENDAR_COLUMNS)].ne(expected[list(repair.CALENDAR_COLUMNS)])
    any_changed = changed.any(axis=1)
    eta_valid = before.eta.notna()
    assert result["changed_rows"] == int(any_changed.sum()) == 6
    assert result["source_eta_non_null_rows"] == int(eta_valid.sum()) == 5
    assert result["eta_non_null_affected_rows"] == int((any_changed & eta_valid).sum()) == 3
    for col in repair.CALENDAR_COLUMNS:
        assert result["changed_rows_by_column"][col] == int(changed[col].sum())
        assert result["eta_non_null_affected_rows_by_column"][col] == int((changed[col] & eta_valid).sum())
    for date, counts in result["changed_rows_by_date"].items():
        key = pd.Timestamp(date)
        assert counts["changed_rows"] == int(any_changed.loc[key].sum())
        assert counts["eta_non_null_affected_rows"] == int((any_changed & eta_valid).loc[key].sum())
        assert counts["by_column"] == {col: int(changed[col].loc[key].sum())
                                       for col in repair.CALENDAR_COLUMNS}
    assert result["source_sha256"] == original_hash == _digest(source)
    assert result["output_sha256"] == _digest(output)
    assert result["verification"]["all_rows_checked"] == 8
    assert result["verification"]["schema_and_index_metadata_equal"]
    assert result["verification"]["non_calendar_columns_bitwise_equal"]
    assert result["verification"]["calendar_columns_match_recomputed_flags"]
    assert json.loads(Path(result["provenance"]).read_text()) == result
    assert set(p.name for p in source.parent.iterdir()) == {
        "source.parquet", "rebuilt.parquet", "rebuilt.parquet.provenance.json"}


def test_dry_run_reads_footer_only_and_writes_nothing(source, monkeypatch):
    output = source.parent / "absent" / "rebuilt.parquet"
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run must not hash or scan the full source")
    monkeypatch.setattr(repair, "_sha256", forbidden)
    monkeypatch.setattr(repair, "_flags_for_source", forbidden)
    result = repair.rebuild(source, output, dry_run=True)
    assert result["status"] == "dry_run"
    assert not output.parent.exists()


def test_existing_output_or_provenance_is_never_overwritten(source):
    output = source.with_name("rebuilt.parquet")
    output.write_bytes(b"existing file")
    with pytest.raises(FileExistsError):
        repair.rebuild(source, output)
    assert output.read_bytes() == b"existing file"
    output.unlink()
    provenance = output.with_suffix(".parquet.provenance.json")
    provenance.write_bytes(b"existing audit")
    with pytest.raises(FileExistsError):
        repair.rebuild(source, output)
    assert provenance.read_bytes() == b"existing audit"
    with pytest.raises(ValueError, match="differ from immutable source"):
        repair.rebuild(source, source)


def test_verification_failure_leaves_source_untouched_and_no_output(source, monkeypatch):
    output = source.with_name("rebuilt.parquet")
    source_hash = _digest(source)
    def fail(*args, **kwargs):
        raise ValueError("injected validation failure")
    monkeypatch.setattr(repair, "_verify_output", fail)
    with pytest.raises(ValueError, match="injected validation"):
        repair.rebuild(source, output, batch_size=2)
    assert _digest(source) == source_hash
    assert list(source.parent.iterdir()) == [source]


def test_source_mutation_detection_prevents_publication(source, monkeypatch):
    output = source.with_name("rebuilt.parquet")
    real_sha256 = repair._sha256
    calls = 0
    def changed_hash(path):
        nonlocal calls
        if Path(path) == source:
            calls += 1
            return "a" if calls == 1 else "b"
        return real_sha256(path)
    monkeypatch.setattr(repair, "_sha256", changed_hash)
    with pytest.raises(RuntimeError, match="source changed"):
        repair.rebuild(source, output, batch_size=2)
    assert list(source.parent.iterdir()) == [source]


def test_publication_collision_keeps_other_writers_file(source, monkeypatch):
    output = source.with_name("rebuilt.parquet")
    provenance = output.with_suffix(".parquet.provenance.json")
    real_link = repair.os.link
    def race_link(src, dst):
        if Path(dst) == provenance:
            provenance.write_text("another writer")
        return real_link(src, dst)
    monkeypatch.setattr(repair.os, "link", race_link)
    with pytest.raises(FileExistsError):
        repair.rebuild(source, output, batch_size=2)
    assert not output.exists()
    assert provenance.read_text() == "another writer"
    assert sorted(p.name for p in source.parent.iterdir()) == [
        "rebuilt.parquet.provenance.json", "source.parquet"]


def test_verifier_rejects_non_calendar_corruption_and_signed_zero_change(source):
    flags = repair._flags_for_source(pq.ParquetFile(source), 2)
    output = source.with_name("rebuilt.parquet")
    repair.rebuild(source, output, batch_size=2)
    table = pq.read_table(output)
    i = table.schema.get_field_index("tech_float32")
    values = table.column(i).combine_chunks().to_pylist()
    values[1] = 0.0  # Original -0.0 is numerically equal but bitwise different.
    table = table.set_column(i, table.schema.field(i), pa.array(values, type=pa.float32()))
    pq.write_table(table, output)
    with pytest.raises(ValueError, match="tech_float32"):
        repair._verify_output(pq.ParquetFile(source), output, flags, 3)


def test_cli_emits_provenance_json_for_dry_run(source, capsys):
    output = source.with_name("rebuilt.parquet")
    assert repair.main(["--source", str(source), "--output", str(output), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["source_rows"] == 8
    assert not output.exists()
