#!/usr/bin/env bash
# Read-only: sync PROD fills (owner + mirrored users) into private caches for the frontend publisher.
#   crypto_trading/ops/prod_fill_sync.sh [--full]
set -euo pipefail
cd "$(dirname "$0")/../.."
PYTHONPATH="$PWD" exec conda run -n someopark_run --no-capture-output \
  python -m crypto_trading.ops.prod_fill_sync "$@"
