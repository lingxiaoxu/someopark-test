"""Auth signing tests — no network. Generates a throwaway RSA key and verifies
the PSS signature + header contract that the copied auth.py produces."""
import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from crypto_trading.crypto_common.kalshi.auth import auth_headers, sign


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_sign_verifies_with_public_key(key):
    msg = "1700000000000GET/trade-api/v2/portfolio/balance"
    sig = base64.b64decode(sign(key, msg))
    key.public_key().verify(
        sig, msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())  # raises on mismatch


def test_auth_headers_contract(key):
    h = auth_headers(key, "kid-123", "get", "/trade-api/v2/markets?limit=5&cursor=x")
    assert set(h) == {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                      "KALSHI-ACCESS-SIGNATURE", "Content-Type"}
    assert h["KALSHI-ACCESS-KEY"] == "kid-123"
    ts = int(h["KALSHI-ACCESS-TIMESTAMP"])
    assert ts > 1_700_000_000_000  # milliseconds, not seconds

    # signature must cover the path WITHOUT the query string, method upper-cased
    expected_msg = f"{ts}GET/trade-api/v2/markets"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        sig, expected_msg.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())
