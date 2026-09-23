"""Incremental, read-only W8 settlement ledger reader for isolated W10.

W8 append_tape writes one gzip member per JSON line. Cursors point only at
member boundaries (apart from an explicit registration resynchronization).
No partial member, bad checksum, or invalid settlement advances a cursor.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import zlib


class ArchiveReadError(ValueError):
    """Archive evidence cannot be safely consumed."""


class SettlementTape:
    MAX_READ_BYTES = 8_000_000
    MAX_MEMBER_OUTPUT_BYTES = 32_000_000

    def __init__(self, root):
        self.root = Path(root)
        self.cursors = {}
        self.errors = 0
        self.error_details = []

    def _days(self, now):
        today = datetime.fromtimestamp(float(now), timezone.utc)
        return [self.root / ((today - timedelta(days=n)).strftime('%Y-%m-%d') + '.jsonl.gz')
                for n in (1, 0)]

    def seed_end(self, now):
        """Ignore existing history, including a member being appended now.

        A seed may fall inside a compressed member. In that case discard its
        trailing bytes, retaining enough bytes to recognize the next header.
        """
        for path in self._days(now):
            if path.exists():
                stat = path.stat()
                self.cursors[str(path)] = {'inode': stat.st_ino, 'offset': stat.st_size,
                                           'resync': True}

    def _fail(self, path, offset, reason):
        self.errors += 1
        self.error_details.append({'path': str(path), 'offset': offset, 'reason': str(reason)})
        self.error_details = self.error_details[-100:]
        raise ArchiveReadError(f'{path.name} at compressed offset {offset}: {reason}')

    @staticmethod
    def _header_start(payload):
        position = 0
        while True:
            position = payload.find(b'\x1f\x8b\x08', position)
            if position < 0:
                return None
            # The fixed gzip header is ten bytes; its reserved flag bits are 0.
            if len(payload) - position < 10:
                return None
            if payload[position + 3] & 0xe0 == 0:
                return position
            position += 1

    def _decode_member(self, payload, path, offset):
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError as exc:
            self._fail(path, offset, f'invalid UTF-8: {exc}')
        # append_tape always includes a newline before closing the member.
        if not text.endswith('\n'):
            self._fail(path, offset, 'complete gzip member lacks final JSONL newline')
        result = []
        for line in text.splitlines():
            if not line.strip():
                self._fail(path, offset, 'empty JSONL record')
            try:
                row = json.loads(line, parse_constant=lambda value: self._fail(
                    path, offset, f'non-finite JSON value {value}'))
            except json.JSONDecodeError as exc:
                self._fail(path, offset, f'invalid JSON: {exc}')
            if not isinstance(row, dict):
                self._fail(path, offset, 'archive row must be an object')
            if row.get('kind') == 'settlement' and row.get('book') == 'tilted':
                ledger, summary = row.get('ledger'), row.get('summary')
                if not isinstance(ledger, dict) or not isinstance(summary, dict):
                    self._fail(path, offset, 'tilted settlement requires full ledger and summary')
                if not ledger.get('ticker') or ledger.get('ticker') != summary.get('ticker'):
                    self._fail(path, offset, 'settlement ledger/summary ticker mismatch')
                result.append(row)
        return result

    def _read(self, path):
        if not path.exists():
            return []
        stat = path.stat()
        cursor = deepcopy(self.cursors.get(str(path),
            {'inode': stat.st_ino, 'offset': 0, 'resync': False}))
        offset = cursor.get('offset')
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            self._fail(path, 0, 'invalid saved offset')
        if cursor.get('inode') != stat.st_ino:
            self._fail(path, offset, 'source inode changed; registration must not silently replay replacement')
        if stat.st_size < offset:
            self._fail(path, offset, 'source archive was truncated')
        if stat.st_size == offset:
            self.cursors[str(path)] = cursor
            return []
        with path.open('rb') as stream:
            stream.seek(offset)
            payload = stream.read(self.MAX_READ_BYTES)
        if cursor.get('resync'):
            start = self._header_start(payload)
            if start is None:
                # Preserve up to nine bytes so a header crossing reads is not lost.
                cursor['offset'] += max(0, len(payload) - 9)
                self.cursors[str(path)] = cursor
                return []
            offset += start
            payload = payload[start:]
            cursor.update(offset=offset, resync=False)
        rows = []
        consumed = 0
        while consumed < len(payload):
            member_offset = offset + consumed
            member = payload[consumed:]
            decoder = zlib.decompressobj(wbits=31)
            try:
                plain = decoder.decompress(member, self.MAX_MEMBER_OUTPUT_BYTES + 1)
            except zlib.error as exc:
                self._fail(path, member_offset, f'invalid gzip member/checksum: {exc}')
            if len(plain) > self.MAX_MEMBER_OUTPUT_BYTES or decoder.unconsumed_tail:
                self._fail(path, member_offset, 'gzip member exceeds bounded output limit')
            if not decoder.eof:
                if consumed == 0 and len(member) >= self.MAX_READ_BYTES:
                    self._fail(path, member_offset, 'gzip member exceeds bounded compressed limit')
                break
            rows.extend(self._decode_member(plain, path, member_offset))
            count = len(member) - len(decoder.unused_data)
            if count <= 0:
                self._fail(path, member_offset, 'gzip decoder made no progress')
            consumed += count
            cursor['offset'] = offset + consumed
        self.cursors[str(path)] = cursor
        return rows

    def update(self, now):
        """Return newly complete tilted settlements and advance safe cursors.

        Cursor updates across all files are transactional if decoding fails.
        Save .cursors together with the consumer's derived state after success.
        Older cursor paths are checked too, preserving delayed writes at midnight.
        """
        paths = sorted(set(self._days(now)) | {Path(p) for p in self.cursors})
        if any(path.parent.resolve() != self.root.resolve() for path in paths):
            self._fail(self.root, 0, 'saved cursor escapes archive root')
        original = deepcopy(self.cursors)
        try:
            rows = []
            for path in paths:
                rows.extend(self._read(path))
            return rows
        except Exception:
            self.cursors = original
            raise
