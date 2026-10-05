#!/usr/bin/env bash
# Read-only overview of per-user Kalshi PROD accounts (never changes trading).
#   crypto_trading/ops/kalshi_users.sh status | pending | check
#   crypto_trading/ops/kalshi_users.sh lookup someone@example.com
set -euo pipefail
cd "$(dirname "$0")/../.."
PYTHONPATH="$PWD" exec conda run -n someopark_run --no-capture-output \
  python -m crypto_trading.ops.kalshi_users "$@"
