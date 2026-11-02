import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from combo_edge.kalshi.auth import Signer


def _pem(key) -> bytes:  # type: ignore[no-untyped-def]
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def test_rsa_pss_signature_verifies_and_strips_query() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = Signer("key-123", _pem(key))
    h = s.headers("get", "/trade-api/v2/portfolio/balance?x=1", ts_ms=1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "key-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        sig,
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert s.key_type == "rsa"


def test_ed25519_signature_verifies() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    s = Signer("k", _pem(key))
    h = s.headers("POST", "/trade-api/v2/communications/rfqs", ts_ms=42)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"42POST/trade-api/v2/communications/rfqs")
    assert s.key_type == "ed25519"


def test_websocket_handshake_signs_ws_path() -> None:
    from combo_edge.settings import WS_SIGN_PATH

    key = ed25519.Ed25519PrivateKey.generate()
    h = Signer("k", _pem(key)).headers("GET", WS_SIGN_PATH, ts_ms=7)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"7GET/trade-api/ws/v2")


def test_credentials_errors_never_echo_secrets(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from combo_edge.settings import CredentialError, credentials_for, load_env_settings

    secret_path = tmp_path / "nope.pem"
    monkeypatch.setenv("KALSHI_PROD_API_KEY_ID", "abcd-secret-id")
    monkeypatch.setenv("KALSHI_PROD_PRIVATE_KEY_PATH", str(secret_path))
    try:
        credentials_for("prod", load_env_settings())
    except CredentialError as exc:
        assert "abcd-secret-id" not in str(exc)
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("expected CredentialError")
