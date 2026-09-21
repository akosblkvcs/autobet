"""Sealing a bookmaker password so the database never holds the plaintext."""

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_KEY_BYTES = 32


def key(material: str) -> bytes:
    """The key `ENCRYPTION_KEY` names, as AES-GCM wants it."""
    raw = bytes.fromhex(material)

    if len(raw) != _KEY_BYTES:
        raise ValueError(f"ENCRYPTION_KEY must be {_KEY_BYTES} bytes, as hex characters")

    return raw


def seal(secret: str, material: str) -> bytes:
    """`nonce || ciphertext || tag`, which is what the column stores."""
    nonce = os.urandom(_NONCE_BYTES)

    return nonce + AESGCM(key(material)).encrypt(nonce, secret.encode(), None)


def unseal(sealed: bytes, material: str) -> str:
    """The plaintext back, or an error if the key or the bytes are not ours."""
    nonce, body = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]

    return AESGCM(key(material)).decrypt(nonce, body, None).decode()
