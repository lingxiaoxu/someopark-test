"""DailyJsonlWriter tests — tmp dir, no network."""
import gzip
import json

from crypto_trading.crypto_common import io_jsonl
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter, gzip_in_place


def test_write_and_readback(tmp_path):
    w = DailyJsonlWriter(tmp_path)
    w.write("a/b", {"x": 1})
    w.write("a/b", {"x": 2})
    w.close()
    files = list((tmp_path / "a" / "b").glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(l) for l in files[0].read_text().splitlines()]
    assert [l["x"] for l in lines] == [1, 2]


def test_midnight_rotation_gzips_old_day(tmp_path, monkeypatch):
    days = iter(["2026-07-06", "2026-07-07"])
    current = {"d": next(days)}
    monkeypatch.setattr(io_jsonl, "utc_day", lambda: current["d"])

    w = DailyJsonlWriter(tmp_path)
    w.write("s", {"n": 1})
    current["d"] = next(days)          # midnight passes
    w.write("s", {"n": 2})
    w.close()

    gz = tmp_path / "s" / "2026-07-06.jsonl.gz"
    assert gz.exists()
    assert json.loads(gzip.open(gz).read().splitlines()[0])["n"] == 1
    assert (tmp_path / "s" / "2026-07-07.jsonl").exists()
    assert not (tmp_path / "s" / "2026-07-06.jsonl").exists()


def test_gzip_in_place_missing_file_is_noop(tmp_path):
    gzip_in_place(tmp_path / "nope.jsonl")  # must not raise
