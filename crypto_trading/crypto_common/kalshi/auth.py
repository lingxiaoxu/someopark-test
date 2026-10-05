# COPIED from prediction_market/venues/kalshi/auth.py (read-only template, Plan 00 §4).
# Identical implementation — the margin API uses the same RSA-PSS scheme (probe-verified).
"""Kalshi request signing (plan 01 §2): RSA-PSS/SHA256 for RSA keys, plain Ed25519 for Ed25519 keys
(Kalshi changelog 2026-09-24; the web app issues Ed25519 keys by default since 2026-10-01; same headers
and pre-sign text for both).

Sign string = timestamp(ms) + HTTP_METHOD + path_without_query, signed with
RSA-PSS/SHA256 and base64-encoded. Three headers go on every authenticated
request. Common pitfalls (plan 01 §2.3): timestamp must be MILLISECONDS; the
signed path must NOT include the query string; the path starts at /trade-api/v2.

Market data (markets/orderbook/trades/candlesticks) needs NO auth — only
orders / portfolio / private websocket channels require these headers.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

KalshiPrivateKey = RSAPrivateKey | Ed25519PrivateKey


def load_private_key(path: str | Path) -> KalshiPrivateKey:
    """Load a Kalshi private key from its PEM file (.key/.pem/.txt): RSA (PKCS#1 or PKCS#8) or Ed25519 (PKCS#8)."""
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None, backend=default_backend())
    if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
        raise TypeError("Kalshi key must be an RSA or Ed25519 private key")
    return key


def sign(private_key: KalshiPrivateKey, message: str) -> str:
    """Signature of ``message``, base64-encoded: RSA-PSS/SHA256 for RSA keys, Ed25519 for Ed25519 keys."""
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(message.encode("utf-8"))
    else:
        sig = private_key.sign(
            message.encode("utf-8"),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode("utf-8")


def auth_headers(private_key: KalshiPrivateKey, api_key_id: str, method: str, full_path: str) -> dict[str, str]:
    """Build the three Kalshi auth headers for one request.

    ``full_path`` may include a query string; it is stripped before signing.
    """
    ts = str(int(time.time() * 1000))             # milliseconds (NOT seconds)
    path_no_query = urlparse(full_path).path      # drop ?query
    signature = sign(private_key, f"{ts}{method.upper()}{path_no_query}")
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }
