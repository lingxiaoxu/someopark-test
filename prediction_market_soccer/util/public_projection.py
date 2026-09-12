"""Small public Soccer boards; full quote captures remain private.

This is a serialization projection, never a decision input. It does not mutate
its argument, attest quote availability, change prices, or access a database.
"""
from __future__ import annotations

import hashlib
import json

PUBLIC_BOARDS = frozenset(('upcoming.json', 'inplay_live.json', 'inplay_live_advance.json'))


def project_public_board(document):
    """Return (display document, content-addressed private capture bytes).

    Preserve all display/unknown fields except explicit full receipt containers.
    The in-memory producer and its receipts are unchanged. IDs already carried by
    selections/diagnostics stay public; no private filesystem paths are added.
    """
    captures = {}
    seen = {}

    def keep_capture(value):
        if not isinstance(value, (dict, list)):
            return
        # Container lists are stored as individual captures so a side repeated in
        # quote_receipts, prices and a lock selection has one private object.
        if isinstance(value, list):
            for item in value:
                keep_capture(item)
            return
        key = id(value)
        if key in seen:
            return
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        digest = hashlib.sha256(payload).hexdigest()
        captures[digest] = payload
        seen[key] = digest

    def project(value):
        if isinstance(value, dict):
            # A new container name must not accidentally publish a full receipt.
            if {'receipt_id', 'binding', 'raw'}.issubset(value):
                keep_capture(value)
                return {k: value[k] for k in ('receipt_id', 'binding_id') if k in value}
            result = {}
            for key, item in value.items():
                if isinstance(key, str) and (key == 'receipt' or key.endswith('_receipt') or key == 'quote_receipts' or key.endswith('_receipts')):
                    keep_capture(item)
                    continue
                result[key] = project(item)
            return result
        if isinstance(value, (list, tuple)):
            return [project(item) for item in value]
        return value

    return project(document), captures
