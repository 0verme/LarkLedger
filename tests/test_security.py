import base64
import hashlib
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from lark_ledger.services.feishu import decrypt_event, verify_signature


def test_signature_verification() -> None:
    body = b'{"event":"ok"}'
    signature = hashlib.sha256(b"123" + b"nonce" + b"key" + body).hexdigest()
    assert verify_signature(body, "123", "nonce", signature, "key")
    assert not verify_signature(body, "123", "nonce", "bad", "key")


def test_event_decryption() -> None:
    key = "encrypt-key"
    iv = b"0123456789abcdef"
    payload = {"type": "url_verification", "challenge": "abc"}
    padder = PKCS7(128).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)
    ).encryptor()
    encrypted = base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()
    assert decrypt_event(encrypted, key) == payload
