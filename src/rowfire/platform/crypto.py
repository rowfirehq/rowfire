"""Envelope encryption for stored database credentials.

v0 never persisted a DSN: it was read from an environment variable, held in
memory, and never written anywhere. A scheduler cannot work that way -- to poll
on its own it must hold the credential -- so this module is where that property
is deliberately given up, and it is worth being explicit about the trade.

The scheme is envelope encryption even though there is no KMS yet:

    master key (env)  ->  wraps  ->  per-row data key  ->  encrypts  ->  DSN

Using one key directly would be simpler and would make the eventual move to a
cloud KMS a full data migration: every ciphertext would have to be decrypted
and re-encrypted. With an envelope, moving to KMS only rewraps the data keys --
the ciphertext columns are untouched. The columns that make that possible
(`wrapped_data_key`, `key_id`, `algorithm`) are the whole point of doing it
now, while there is no data to migrate.

Algorithm: AES-256-GCM for both layers. Authenticated, so tampering with a
ciphertext fails loudly rather than yielding garbage that gets fed to psycopg.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALGORITHM = "AESGCM-256+envelope-v1"
MASTER_KEY_ENV = "ROWFIRE_MASTER_KEY"
_LOCAL_KEY_ID = "local/env"

# Bound the plaintext so a pathological value cannot be stored as a "DSN".
MAX_SECRET_BYTES = 4096


class CryptoError(Exception):
    """Raised for key and decryption problems. Never contains plaintext."""


@dataclass(frozen=True)
class Envelope:
    """What gets persisted. No field here reveals the secret."""

    ciphertext: bytes
    nonce: bytes
    wrapped_data_key: bytes
    wrap_nonce: bytes
    key_id: str
    algorithm: str = ALGORITHM


def generate_master_key() -> str:
    """A new base64 master key, for putting in the environment."""
    return base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


def load_master_key(env_var: str = MASTER_KEY_ENV) -> bytes:
    raw = os.environ.get(env_var)
    if not raw:
        raise CryptoError(
            f"{env_var} is not set. Generate one with:\n"
            f"  python -c 'from rowfire.platform.crypto import generate_master_key; "
            f"print(generate_master_key())'\n"
            f"Losing this key makes every stored credential unrecoverable."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise CryptoError(f"{env_var} is not valid base64") from exc

    if len(key) != 32:
        raise CryptoError(f"{env_var} must decode to 32 bytes for AES-256, got {len(key)}")
    return key


def encrypt(secret: str, master_key: bytes | None = None, key_id: str = _LOCAL_KEY_ID) -> Envelope:
    """Encrypt a secret under a fresh per-row data key."""
    if not secret:
        raise CryptoError("refusing to encrypt an empty secret")
    plaintext = secret.encode()
    if len(plaintext) > MAX_SECRET_BYTES:
        raise CryptoError(f"secret exceeds {MAX_SECRET_BYTES} bytes")

    master = master_key if master_key is not None else load_master_key()

    # A distinct data key per row: compromising one row's key does not
    # compromise the others, and rotation can be incremental.
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, ALGORITHM.encode())

    wrap_nonce = os.urandom(12)
    wrapped = AESGCM(master).encrypt(wrap_nonce, data_key, key_id.encode())

    return Envelope(
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_data_key=wrapped,
        wrap_nonce=wrap_nonce,
        key_id=key_id,
    )


def decrypt(envelope: Envelope, master_key: bytes | None = None) -> str:
    """Recover the secret. The caller must not log the result."""
    master = master_key if master_key is not None else load_master_key()

    if envelope.algorithm != ALGORITHM:
        raise CryptoError(f"unsupported algorithm {envelope.algorithm!r}")

    try:
        data_key = AESGCM(master).decrypt(
            envelope.wrap_nonce, envelope.wrapped_data_key, envelope.key_id.encode()
        )
    except InvalidTag as exc:
        raise CryptoError(
            "could not unwrap the data key -- wrong master key, or the stored "
            "key material has been altered"
        ) from exc

    try:
        plaintext = AESGCM(data_key).decrypt(
            envelope.nonce, envelope.ciphertext, ALGORITHM.encode()
        )
    except InvalidTag as exc:
        raise CryptoError(
            "could not decrypt the secret -- the ciphertext has been altered"
        ) from exc

    return plaintext.decode()


def rewrap(envelope: Envelope, old_master: bytes, new_master: bytes, new_key_id: str) -> Envelope:
    """Move a row to a new master key without touching its ciphertext.

    This is the migration path to a real KMS: unwrap the data key with the
    current master, wrap it with the new one. The encrypted DSN itself is
    never decrypted and never rewritten.
    """
    try:
        data_key = AESGCM(old_master).decrypt(
            envelope.wrap_nonce, envelope.wrapped_data_key, envelope.key_id.encode()
        )
    except InvalidTag as exc:
        raise CryptoError("could not unwrap with the old master key") from exc

    wrap_nonce = os.urandom(12)
    wrapped = AESGCM(new_master).encrypt(wrap_nonce, data_key, new_key_id.encode())

    return Envelope(
        ciphertext=envelope.ciphertext,
        nonce=envelope.nonce,
        wrapped_data_key=wrapped,
        wrap_nonce=wrap_nonce,
        key_id=new_key_id,
        algorithm=envelope.algorithm,
    )
