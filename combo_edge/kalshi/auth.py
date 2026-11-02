"""Request signing, per docs.kalshi.com (verified 2026-11-02).

Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE.
Signed text: ``timestamp_ms + METHOD + path`` where path includes ``/trade-api/v2`` and excludes the
query string. RSA keys sign with RSA-PSS / SHA-256 / MGF1-SHA-256 / salt = digest length. Ed25519 keys
sign the text directly. The websocket handshake signs ``GET /trade-api/ws/v2``.
"""

from __future__ import annotations

import base64
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


def load_private_key(pem: bytes) -> Any:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise ValueError("unsupported key type; Kalshi accepts RSA or Ed25519 keys")
    return key


def sign_text(key: Any, message: str) -> str:
    data = message.encode("utf-8")
    if isinstance(key, ed25519.Ed25519PrivateKey):
        sig = key.sign(data)
    else:
        sig = key.sign(
            data,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode("ascii")


class Signer:
    def __init__(self, key_id: str, private_key_pem: bytes):
        self.key_id = key_id
        self._key = load_private_key(private_key_pem)

    @property
    def key_type(self) -> str:
        return "ed25519" if isinstance(self._key, ed25519.Ed25519PrivateKey) else "rsa"

    def headers(self, method: str, full_path: str, ts_ms: int | None = None) -> dict[str, str]:
        path = full_path.split("?", 1)[0]
        ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign_text(self._key, ts + method.upper() + path),
        }
