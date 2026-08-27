# venues/ — venue abstraction layer

Unified `Venue` interface (`base.py`) with three concrete backends. Execution is
hard-restricted to Kalshi + Polymarket US by `guard.py` (plan 05/09).

## Status

| module | status | needs |
|---|---|---|
| `base.py` | ✅ done — `Venue`/`ExecutionVenue` interface + `OrderBook`/`Balance`/`Position` types | — |
| `guard.py` | ✅ done — `assert_executable` blocks orders to non-executable venues | — |
| `kalshi/auth.py` | ✅ done — RSA-PSS signer (plan 01 §2) | API key + PEM to exercise live |
| `kalshi/market_data.py` | ✅ done — public reader; `best_prices` parser unit-tested | network for live calls (no auth) |
| `kalshi/` orders/ws | ⏳ TODO | Kalshi API key (Demo first) |
| `polymarket_us/` | ⏳ TODO — Ed25519 client, intent orders, WS | KYC + Ed25519 creds, `polymarket-us` SDK |
| `polymarket_global/` | ⏳ TODO — read-only Gamma/Data/CLOB reference | `py-clob-client-v2` |

## Why orders/WS are deferred

Per the roadmap (plan 06 stage 0), order placement and private WebSocket
channels require KYC + credentials and must be proven on **Kalshi Demo /
Polymarket small-size first**. Building untested order clients now would violate
the "Demo-green before prod" discipline. The interfaces and the **no-auth public
market-data reader** (testable today) are in place so the data/strategy layers
can be developed against real books immediately.

The engine always works in **target net positions** (plan 09 §5); each venue
translates a target into its own legal primitives (Kalshi netting / Polymarket
US `SELL_*` intents) — never "buy another lot" blindly.
