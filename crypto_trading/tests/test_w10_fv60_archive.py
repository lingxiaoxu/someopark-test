"""W8 archive tail tests use synthetic files only; no live services or data."""
from datetime import datetime, timezone
import gzip
import json

import pytest

from crypto_trading.crypto_strategies.w10_entry_paper.archive_tail import (
    ArchiveReadError, SettlementTape)


NOW = datetime(2026, 9, 17, 0, 1, tzinfo=timezone.utc).timestamp()


def row(ticker='BTC', book='tilted', kind='settlement'):
    return {'kind': kind, 'book': book, 'summary': {'ticker': ticker},
            'ledger': {'ticker': ticker, 'orders': [], 'fills': []}}


def member(value):
    return gzip.compress((json.dumps(value) + '\n').encode())


def append(path, data):
    with path.open('ab') as stream:
        stream.write(data)


def test_incremental_members_only_tilted_settlement(tmp_path):
    path = tmp_path/'2026-09-17.jsonl.gz'
    path.write_bytes(member(row('old')))
    tape = SettlementTape(tmp_path)
    tape.seed_end(NOW)
    append(path, member(row('paired', book='paired')) + member(row('tick', kind='tick')))
    append(path, member(row('new')))
    assert [r['ledger']['ticker'] for r in tape.update(NOW)] == ['new']
    assert tape.update(NOW) == []
    assert tape.cursors[str(path)]['offset'] == path.stat().st_size


def test_partial_member_does_not_advance_then_finishes_once(tmp_path):
    path = tmp_path/'2026-09-17.jsonl.gz'
    data = member(row())
    path.write_bytes(data[:-6])
    tape = SettlementTape(tmp_path)
    assert tape.update(NOW) == []
    assert tape.cursors[str(path)]['offset'] == 0
    append(path, data[-6:])
    assert tape.update(NOW) == [row()]
    assert tape.update(NOW) == []


def test_complete_member_before_partial_is_committed(tmp_path):
    path = tmp_path/'2026-09-17.jsonl.gz'
    first, second = member(row('A')), member(row('B'))
    path.write_bytes(first + second[:17])
    tape = SettlementTape(tmp_path)
    assert tape.update(NOW) == [row('A')]
    assert tape.cursors[str(path)]['offset'] == len(first)
    append(path, second[17:])
    assert tape.update(NOW) == [row('B')]


@pytest.mark.parametrize('seed_cut', [2, 10, 20, -8, -1])
def test_registration_inside_member_ignores_it_until_next_header(tmp_path, seed_cut):
    path = tmp_path/'2026-09-17.jsonl.gz'
    history, new = member(row('historical')), member(row('prospective'))
    path.write_bytes(history[:seed_cut])
    tape = SettlementTape(tmp_path)
    tape.seed_end(NOW)
    append(path, history[seed_cut:])
    assert tape.update(NOW) == []
    append(path, new[:4])
    assert tape.update(NOW) == []
    append(path, new[4:])
    assert tape.update(NOW) == [row('prospective')]


def test_restart_json_cursor_and_midnight_delayed_old_day_append(tmp_path):
    yesterday = tmp_path/'2026-09-16.jsonl.gz'
    today = tmp_path/'2026-09-17.jsonl.gz'
    yesterday.write_bytes(member(row('old')))
    tape = SettlementTape(tmp_path)
    tape.seed_end(NOW - 120)
    restored = SettlementTape(tmp_path)
    restored.cursors = json.loads(json.dumps(tape.cursors))
    append(yesterday, member(row('late_previous_day')))
    today.write_bytes(member(row('new_day')))
    assert [r['ledger']['ticker'] for r in restored.update(NOW)] == ['late_previous_day', 'new_day']
    assert restored.update(NOW) == []


@pytest.mark.parametrize('bad', [b'{bad json}\n', b'[]\n', b'{"x": NaN}\n', b'{}', b'\xff\n'])
def test_invalid_member_fails_closed_and_cursors_transactional(tmp_path, bad):
    path = tmp_path/'2026-09-17.jsonl.gz'
    path.write_bytes(member(row('good')) + gzip.compress(bad))
    tape = SettlementTape(tmp_path)
    with pytest.raises(ArchiveReadError):
        tape.update(NOW)
    assert tape.cursors == {}
    assert tape.errors == 1
    assert tape.error_details


def test_crc_failure_and_corrupt_settlement_are_not_skipped(tmp_path):
    path = tmp_path/'2026-09-17.jsonl.gz'
    corrupted = bytearray(member(row()))
    corrupted[-8] ^= 1
    path.write_bytes(corrupted)
    tape = SettlementTape(tmp_path)
    with pytest.raises(ArchiveReadError, match='checksum'):
        tape.update(NOW)
    assert tape.cursors == {}
    invalid = row()
    del invalid['ledger']
    path.write_bytes(member(invalid))
    with pytest.raises(ArchiveReadError, match='full ledger'):
        tape.update(NOW)
    assert tape.cursors == {}


def test_multiple_files_rollback_together_on_error(tmp_path):
    (tmp_path/'2026-09-16.jsonl.gz').write_bytes(member(row('good')))
    (tmp_path/'2026-09-17.jsonl.gz').write_bytes(gzip.compress(b'{bad}\n'))
    tape = SettlementTape(tmp_path)
    with pytest.raises(ArchiveReadError):
        tape.update(NOW)
    assert tape.cursors == {}


def test_truncation_and_replacement_fail_closed(tmp_path):
    path = tmp_path/'2026-09-17.jsonl.gz'
    path.write_bytes(member(row()))
    tape = SettlementTape(tmp_path)
    assert tape.update(NOW) == [row()]
    saved = json.loads(json.dumps(tape.cursors))
    path.write_bytes(b'')
    with pytest.raises(ArchiveReadError, match='truncated'):
        tape.update(NOW)
    assert tape.cursors == saved
    replacement = tmp_path/'replacement'
    replacement.write_bytes(member(row('replace')))
    replacement.replace(path)
    with pytest.raises(ArchiveReadError, match='inode changed'):
        tape.update(NOW)
    assert tape.cursors == saved


def test_bounded_read_resumes_multiple_members(tmp_path, monkeypatch):
    path = tmp_path/'2026-09-17.jsonl.gz'
    first, second = member(row('A')), member(row('B'))
    path.write_bytes(first + second)
    tape = SettlementTape(tmp_path)
    monkeypatch.setattr(tape, 'MAX_READ_BYTES', len(first) + 8)
    assert tape.update(NOW) == [row('A')]
    assert tape.update(NOW) == [row('B')]
    assert tape.update(NOW) == []


def test_cursor_cannot_escape_root(tmp_path):
    tape = SettlementTape(tmp_path/'archive')
    tape.cursors[str(tmp_path/'outside.jsonl.gz')] = {'inode': 1, 'offset': 0, 'resync': False}
    with pytest.raises(ArchiveReadError, match='escapes'):
        tape.update(NOW)
