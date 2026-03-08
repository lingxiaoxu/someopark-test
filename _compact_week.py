import pandas as pd, json, os, glob, hashlib

WEEK = '2026-03-02'
DATA_DIR = 'price_data'
WEEK_DIR = os.path.join(DATA_DIR, 'week_' + WEEK)
INDEX_PATH = os.path.join(DATA_DIR, 'index.json')

files = sorted(glob.glob(os.path.join(WEEK_DIR, '*.parquet')))
print('Files to merge:', len(files))

# Merge all, dedup columns (keep='last' → prefer files fetched later, more complete)
frames = [pd.read_parquet(f) for f in files]
combined = pd.concat(frames, axis=1)
combined = combined.loc[:, ~combined.columns.duplicated(keep='last')]
combined = combined.sort_index(axis=1)

symbols = sorted(combined.columns.get_level_values(1).unique().tolist())
print('Symbols (%d): %s' % (len(symbols), symbols))
print('Shape:', combined.shape)

# Compute hash the same way PriceDataStore does
key = '|'.join(symbols) + '|' + WEEK
new_hash = hashlib.md5(key.encode()).hexdigest()[:12]
new_filename = 'week_' + WEEK + '/' + new_hash + '.parquet'
new_filepath = os.path.join(DATA_DIR, new_filename)
print('New hash:', new_hash)

# Write merged parquet
combined.to_parquet(new_filepath, engine='pyarrow')
print('Written:', new_filepath)

# Update index.json
with open(INDEX_PATH) as f:
    index = json.load(f)

old_hashes = {os.path.basename(f).replace('.parquet', '') for f in files}

for h in old_hashes:
    if h != new_hash and h in index.get('files', {}):
        del index['files'][h]
        print('Removed files entry:', h)

index.setdefault('files', {})[new_hash] = {
    'symbols': symbols,
    'week_start': WEEK,
    'filename': new_filename,
    'row_count': len(combined),
}

sym_idx = index.setdefault('symbol_index', {})
for sym in symbols:
    sym_idx.setdefault(sym, {})[WEEK] = new_hash

with open(INDEX_PATH, 'w') as f:
    json.dump(index, f, indent=2)
print('index.json updated')

# Delete old parquet files
deleted = 0
for fname in files:
    if os.path.abspath(fname) != os.path.abspath(new_filepath):
        os.remove(fname)
        deleted += 1
print('Deleted %d old parquet files' % deleted)
print('Done.')
