"""Passphrase-encrypted export/import payloads for gity.

Export file format (binary):
    MAGIC "GITY1" | salt(16) | nonce(12) | AES-256-GCM ciphertext

The plaintext is a JSON document:
    {"version": 1, "config": {...}, "files": {"<key file name>": "<base64>"}}
"""

from __future__ import annotations

import getpass
import os
import sys

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"GITY1"
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
ITERATIONS = 600_000
_HEADER_LEN = len(MAGIC) + SALT_LEN + NONCE_LEN


class WrongPassphrase(Exception):
    """Decryption failed: wrong passphrase, or the file was modified."""


def is_export_blob(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt, iterations=ITERATIONS)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_bytes(passphrase: str, plaintext: bytes) -> bytes:
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return MAGIC + salt + nonce + ct


def decrypt_bytes(passphrase: str, blob: bytes) -> bytes:
    if not is_export_blob(blob):
        raise ValueError("not a gity export file (missing GITY1 header)")
    if len(blob) < _HEADER_LEN + TAG_LEN:
        raise ValueError("gity export file is truncated")
    salt = blob[len(MAGIC):len(MAGIC) + SALT_LEN]
    nonce = blob[len(MAGIC) + SALT_LEN:_HEADER_LEN]
    ct = blob[_HEADER_LEN:]
    key = _derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag:
        raise WrongPassphrase("wrong passphrase or corrupt file") from None


def _ask(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError:
        print("\ngity: no passphrase given", file=sys.stderr)
        sys.exit(1)


def ask_passphrase(confirm: bool = True) -> str:
    p1 = _ask("Export passphrase (protects your private keys): ")
    if not p1:
        print("gity: passphrase cannot be empty", file=sys.stderr)
        sys.exit(1)
    if confirm:
        p2 = _ask("Repeat passphrase: ")
        if p1 != p2:
            print("gity: passphrases do not match", file=sys.stderr)
            sys.exit(1)
    return p1


def ask_decrypt_passphrase() -> str:
    return _ask("Export passphrase: ")
