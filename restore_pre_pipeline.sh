#!/bin/bash
# restore_pre_pipeline.sh
# Restores yesterday's final pair universe files from history back to the main directory,
# so that pre_pipeline.sh (Step 1+2) can be re-run cleanly.
#
# Logic:
#   - In pair_universe_history/, find the files with TODAY's date prefix
#     whose timestamp suffix is the SMALLEST (i.e., earliest today = moved at Step 1 start)
#   - Copy them back to the main directory as pair_universe_mrpt.json / pair_universe_mtfs.json
#   - Delete the current pair_universe_mrpt.json / pair_universe_mtfs.json in main dir first

REPO=/Users/xuling/code/someopark-test
HISTORY=$REPO/pair_universe_history
TODAY=$(date '+%Y%m%d')

echo "[$(date '+%H:%M:%S')] === RESTORE PRE-PIPELINE ==="
echo "[$(date '+%H:%M:%S')] Looking for today's ($TODAY) earliest files in history..."

# Find today's MRPT file with the smallest timestamp (earliest = yesterday's final version)
MRPT_FILE=$(ls "$HISTORY/pair_universe_mrpt_${TODAY}"_*.json 2>/dev/null | sort | head -1)
MTFS_FILE=$(ls "$HISTORY/pair_universe_mtfs_${TODAY}"_*.json 2>/dev/null | sort | head -1)

if [ -z "$MRPT_FILE" ] || [ -z "$MTFS_FILE" ]; then
    echo "ERROR: Could not find today's ($TODAY) pair universe files in history."
    echo "  MRPT found: ${MRPT_FILE:-NONE}"
    echo "  MTFS found: ${MTFS_FILE:-NONE}"
    exit 1
fi

echo "[$(date '+%H:%M:%S')] Found MRPT: $(basename $MRPT_FILE)"
echo "[$(date '+%H:%M:%S')] Found MTFS: $(basename $MTFS_FILE)"

# Delete current files in main dir
echo "[$(date '+%H:%M:%S')] Removing current pair_universe_mrpt.json and pair_universe_mtfs.json..."
rm -f "$REPO/pair_universe_mrpt.json"
rm -f "$REPO/pair_universe_mtfs.json"

# Copy back and rename (strip timestamp suffix)
echo "[$(date '+%H:%M:%S')] Restoring files to main directory..."
cp "$MRPT_FILE" "$REPO/pair_universe_mrpt.json"
cp "$MTFS_FILE" "$REPO/pair_universe_mtfs.json"

echo "[$(date '+%H:%M:%S')] Done. Restored:"
echo "  $REPO/pair_universe_mrpt.json (from $(basename $MRPT_FILE))"
echo "  $REPO/pair_universe_mtfs.json (from $(basename $MTFS_FILE))"
echo ""
echo "[$(date '+%H:%M:%S')] You can now run: bash pre_pipeline.sh"
