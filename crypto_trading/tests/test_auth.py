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


def test_ed25519_keys_sign_with_plain_ed25519_and_load_from_pkcs8_pem(tmp_path):
    """Kalshi issues Ed25519 keys by default since 2026-10-01 (downloaded as <name>.txt, PKCS#8 PEM inside)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from crypto_trading.crypto_common.kalshi.auth import load_private_key
    k = Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----") and len(pem) < 200
    p = tmp_path / "SomeoParkTestKey.txt"; p.write_bytes(pem)
    loaded = load_private_key(p); assert isinstance(loaded, Ed25519PrivateKey)
    h = auth_headers(loaded, "kid-ed", "post", "/trade-api/v2/portfolio/orders?x=1")
    msg = f"{h['KALSHI-ACCESS-TIMESTAMP']}POST/trade-api/v2/portfolio/orders".encode()
    k.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg)       # raises on mismatch
    assert len(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])) == 64                   # a raw Ed25519 signature, not RSA


def test_rsa_pkcs1_pem_still_loads(tmp_path, key):
    """The owner's key is an older RSA key in PKCS#1 ('BEGIN RSA PRIVATE KEY') form."""
    from cryptography.hazmat.primitives import serialization
    from crypto_trading.crypto_common.kalshi.auth import load_private_key
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
    assert pem.startswith(b"-----BEGIN RSA PRIVATE KEY-----")
    p = tmp_path / "kalshi.key"; p.write_bytes(pem)
    assert isinstance(load_private_key(p), rsa.RSAPrivateKey)
