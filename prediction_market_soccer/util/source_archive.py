"""Relocate raw evidence without rewriting a byte of an immutable source DB.

The archive is an explicit content-addressed copy. Its index is derived from the
source rows, not supplied evidence timestamps. Copying never creates availability.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from prediction_market_soccer.util.research_inputs import _path, canonical, digest

KIND = 'soccer_source_raw_archive_v1'


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def expected_objects(conn):
    """Independently bind every raw object to the unchanged source row payload."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {}

    def add(ref, body):
        if not isinstance(ref, str) or not Path(ref).is_absolute():
            raise ValueError('Source raw reference must be an absolute path')
        value = {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
        if ref in expected and expected[ref] != value:
            raise ValueError('One source raw path claims different immutable contents')
        expected[ref] = value

    if 'source_observation_v1' in tables:
        for row in conn.execute('SELECT source,payload,payload_hash,raw_ref FROM source_observation_v1 WHERE raw_ref IS NOT NULL'):
            source, text, payload_hash, ref = row
            payload = json.loads(text)
            if digest(payload) != payload_hash:
                raise ValueError('Source observation payload checksum differs')
            raw = payload['prior'] if source == 'derived:prior' else payload
            add(ref, canonical(raw).encode())
    if 'quote_receipt_v1' in tables:
        for text, payload_hash, ref in conn.execute('SELECT payload,payload_hash,raw_ref FROM quote_receipt_v1'):
            if hashlib.sha256(text.encode()).hexdigest() != payload_hash:
                raise ValueError('Source quote payload checksum differs')
            payload = json.loads(text)
            add(ref, canonical(payload['raw']).encode())
    return dict(sorted(expected.items()))


def build_raw_archive(source, *, root, output_dir, allowed_raw_roots):
    """Copy only declared source objects from explicit allowed directories.

    This is an operator archival action. Offline tests use local fixture roots;
    a real archive requires an explicit read-authorized source directory list.
    No DB rows, paths in DB rows, source times, or availability markers are changed.
    """
    source.assert_unchanged()
    root = Path(root).resolve(strict=True)
    directory = _path(Path(output_dir), root)
    allowed = tuple(Path(p).resolve(strict=True) for p in allowed_raw_roots)
    if not allowed or any(not p.is_dir() for p in allowed):
        raise ValueError('Explicit existing raw source directories are required')
    expected = expected_objects(source.conn)
    # Validate the whole copy plan before creating the archive directory.
    paths = {}
    for ref, meta in expected.items():
        path = Path(ref)
        resolved = path.resolve(strict=True)
        if path != resolved or not path.is_file() or not any(path.is_relative_to(p) for p in allowed):
            raise ValueError('Source raw path is outside the explicit allowed roots')
        if path.stat().st_size != meta['bytes'] or _sha(path) != meta['sha256']:
            raise ValueError('Source raw contents differ from the immutable DB')
        paths[ref] = path
    directory.mkdir(parents=True, exist_ok=False)
    objects = {}
    for ref, meta in expected.items():
        name = 'objects/' + meta['sha256'] + '.json'
        target = directory / name
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            body = paths[ref].read_bytes()
            if hashlib.sha256(body).hexdigest() != meta['sha256']:
                raise ValueError('Source raw changed during archive copy')
            with target.open('xb') as stream:
                stream.write(body)
            target.chmod(0o400)
        objects[ref] = {**meta, 'path': name}
    manifest = {'schema_version': 1, 'kind': KIND,
                'source_sha256': source.manifest['sha256'],
                'copied_at': datetime.now(timezone.utc).isoformat(),
                'objects': objects, 'availability_created': False}
    manifest['archive_id'] = digest(manifest)
    path = directory / 'manifest.json'
    with path.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
    path.chmod(0o400)
    source.assert_unchanged()
    reference = {'path': str(path), 'sha256': _sha(path)}
    RawArchive(reference, root=root, source_sha256=source.manifest['sha256'], conn=source.conn)
    return reference


class RawArchive:
    def __init__(self, reference, *, root, source_sha256, conn):
        if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
            raise ValueError('Explicit archive path and checksum are required')
        self.reference = dict(reference)
        self.root = Path(root).resolve(strict=True)
        self.path = _path(Path(reference['path']), self.root, exists=True)
        self.directory = self.path.parent
        if _sha(self.path) != reference['sha256']:
            raise ValueError('Raw archive manifest checksum differs')
        doc = json.loads(self.path.read_text())
        if (doc.get('kind') != KIND or doc.get('schema_version') != 1
                or doc.get('source_sha256') != source_sha256
                or doc.get('availability_created') is not False
                or doc.get('archive_id') != digest({k: v for k, v in doc.items() if k != 'archive_id'})):
            raise ValueError('Raw archive source or content identity differs')
        expected = expected_objects(conn)
        self.objects = doc.get('objects')
        if not isinstance(self.objects, dict) or set(self.objects) != set(expected):
            raise ValueError('Raw archive omits or invents source references')
        for ref, meta in expected.items():
            actual = self.objects[ref]
            required = {**meta, 'path': 'objects/' + meta['sha256'] + '.json'}
            if actual != required:
                raise ValueError('Raw archive item differs from original source content')
            self.resolve(ref)

    def resolve(self, original_ref):
        if _sha(self.path) != self.reference['sha256']:
            raise ValueError('Raw archive manifest changed')
        if original_ref not in self.objects:
            raise ValueError('Raw reference is not in the bound source archive')
        meta = self.objects[original_ref]
        path = _path(self.directory / meta['path'], self.directory, exists=True)
        if not path.is_file() or path.stat().st_size != meta['bytes'] or _sha(path) != meta['sha256']:
            raise ValueError('Archived raw object is missing or changed')
        return path
