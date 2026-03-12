"""
compact_price_data.py — Merge per-symbol parquet files into one file per week.

Fixes bugs in _compact_week.py:
  - Uses SHA256 (matching PriceDataStore._compute_hash exactly)
  - Uses correct key format: ','.join(sorted_symbols) + '|' + week_start
  - Processes ALL weeks in the index, not just one hardcoded week
  - Fixes single-file weeks whose hash was written with the old MD5 scheme
  - Cleans up orphaned parquet files not tracked in the index
  - Atomic index write (temp file then rename) to prevent corruption on crash
"""

import os
import json
import glob
import hashlib
import tempfile
import shutil
import pandas as pd

DATA_DIR = 'price_data'
INDEX_PATH = os.path.join(DATA_DIR, 'index.json')


def compute_hash(sorted_symbols, week_start_str):
    """Must match PriceDataStore._compute_hash exactly."""
    key = ','.join(sorted_symbols) + '|' + week_start_str
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def load_index():
    with open(INDEX_PATH) as f:
        return json.load(f)


def save_index_atomic(index):
    """Write to temp file then rename — prevents corruption on crash."""
    tmp_fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix='.json.tmp')
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(index, f, indent=2)
        shutil.move(tmp_path, INDEX_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def compact_all():
    index = load_index()
    files_map = index.get('files', {})
    sym_idx = index.get('symbol_index', {})

    # Group hashes by week
    weeks: dict[str, list[str]] = {}
    for h, fe in files_map.items():
        w = fe['week_start']
        weeks.setdefault(w, []).append(h)

    def week_needs_work(week_start, hashes):
        """True if week has multiple files OR single file with wrong hash."""
        if len(hashes) > 1:
            return True
        h = hashes[0]
        fe = files_map[h]
        expected = compute_hash(sorted(fe['symbols']), week_start)
        return expected != h

    needs_work = {w: hs for w, hs in weeks.items() if week_needs_work(w, hs)}
    already_ok = len(weeks) - len(needs_work)
    print(f'Weeks total:           {len(weeks)}')
    print(f'Already correct:       {already_ok}')
    print(f'Need merging/rehash:   {len(needs_work)}')

    merged_count = 0
    deleted_files = 0

    for week_start, hashes in sorted(needs_work.items()):
        # Load all parquet files for this week
        frames = []
        all_filepaths = []
        for h in hashes:
            fe = files_map[h]
            filepath = os.path.join(DATA_DIR, fe['filename'])
            if not os.path.exists(filepath):
                print(f'  WARNING: missing file {filepath} (skipping hash {h})')
                continue
            df = pd.read_parquet(filepath)
            frames.append(df)
            all_filepaths.append(os.path.abspath(filepath))

        if not frames:
            print(f'  SKIP {week_start}: no readable files')
            continue

        # Merge columns; dedup keeps last (later fetches are more complete)
        if len(frames) == 1:
            combined = frames[0]
        else:
            combined = pd.concat(frames, axis=1)
            combined = combined.loc[:, ~combined.columns.duplicated(keep='last')]
        combined = combined.sort_index(axis=1)

        symbols = sorted(combined.columns.get_level_values(1).unique().tolist())

        # Compute correct hash matching PriceDataStore
        new_hash = compute_hash(symbols, week_start)
        new_filename = f'week_{week_start}/{new_hash}.parquet'
        new_filepath = os.path.abspath(os.path.join(DATA_DIR, new_filename))

        os.makedirs(os.path.dirname(new_filepath), exist_ok=True)
        combined.to_parquet(new_filepath, engine='pyarrow')

        # Update index: remove old entries, add new consolidated one
        for h in hashes:
            if h != new_hash and h in files_map:
                del files_map[h]

        files_map[new_hash] = {
            'symbols': symbols,
            'week_start': week_start,
            'filename': new_filename,
            'row_count': len(combined),
        }

        # Point all symbols to the new hash; remove stale hash refs
        for sym in symbols:
            sym_idx.setdefault(sym, {})[week_start] = new_hash
        # Clean up symbol_index entries that still point to deleted hashes
        deleted_hashes = {h for h in hashes if h != new_hash}
        for sym, week_map in sym_idx.items():
            for wk, h in list(week_map.items()):
                if h in deleted_hashes:
                    week_map[wk] = new_hash

        # Delete old parquet files (skip if the new file reuses one of the old paths)
        for fp in all_filepaths:
            if fp != new_filepath:
                os.remove(fp)
                deleted_files += 1

        merged_count += 1
        print(f'  {week_start}: {len(hashes)} file(s) → {new_hash} '
              f'({len(symbols)} syms, {len(combined)} rows)')

    # Save updated index atomically
    index['files'] = files_map
    index['symbol_index'] = sym_idx
    save_index_atomic(index)
    print(f'\nDone. Processed {merged_count} weeks, deleted {deleted_files} old files.')

    # Final integrity check: every symbol_index entry must point to a valid files_map hash
    integrity_errors = 0
    for sym, week_map in sym_idx.items():
        for wk, h in week_map.items():
            if h not in files_map:
                print(f'  INTEGRITY ERROR: {sym}/{wk} → {h} not in files_map')
                integrity_errors += 1
    if integrity_errors == 0:
        print('Index integrity check: OK (all symbol_index entries reference valid files)')
    else:
        print(f'Index integrity check: {integrity_errors} ERRORS — manual fix needed')

    # Report + optionally delete orphaned parquet files
    tracked_abs = {os.path.abspath(os.path.join(DATA_DIR, fe['filename']))
                   for fe in files_map.values()}
    all_on_disk = {os.path.abspath(p)
                   for p in glob.glob(os.path.join(DATA_DIR, '**/*.parquet'), recursive=True)}
    orphans = sorted(all_on_disk - tracked_abs)
    if orphans:
        print(f'\nOrphaned parquet files not in index: {len(orphans)}')
        for p in orphans:
            print(f'  {os.path.relpath(p)}')
        answer = input('\nDelete orphaned files? [y/N] ').strip().lower()
        if answer == 'y':
            for p in orphans:
                os.remove(p)
                print(f'  Deleted: {os.path.relpath(p)}')
    else:
        print('No orphaned parquet files.')


if __name__ == '__main__':
    compact_all()
