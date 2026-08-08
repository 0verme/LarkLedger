"""Signature verification and event decryption for Feishu webhooks (split from ``feishu.py``)."""

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def verify_signature(raw_body: bytes, timestamp: str, nonce: str, signature: str, key: str) -> bool:
    material = timestamp.encode() + nonce.encode() + key.encode() + raw_body
    expected = hashlib.sha256(material).hexdigest()
    return hmac.compare_digest(expected, signature)

def decrypt_event(encrypted: str, key: str) -> dict[str, Any]:
    digest = hashlib.sha256(key.encode()).digest()
    raw = base64.b64decode(encrypted)
    if len(raw) < 32 or len(raw) % 16:
        raise ValueError("invalid encrypted event")
    decryptor = Cipher(algorithms.AES(digest), modes.CBC(raw[:16])).decryptor()
    padded = decryptor.update(raw[16:]) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    result = json.loads(plain)
    if not isinstance(result, dict):
        raise ValueError("decrypted event is not an object")
    return result
