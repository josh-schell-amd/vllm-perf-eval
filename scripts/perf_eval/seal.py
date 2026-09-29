#!/usr/bin/env python3
"""Encrypt the published payload so only someone with the dashboard login can
read it, and decrypt it again for the deploy's change check.

The site is public and static, so the login cannot be checked by a server.
Instead the payload is sealed with a key derived from the username and
password, and the page derives the same key from what the user types. The
page's side is site/js/unlock.js; the two must agree on every parameter here.

Credentials come from DASHBOARD_USERNAME and DASHBOARD_PASSWORD.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1
KDF = "PBKDF2-SHA256"
# OWASP's recommendation for PBKDF2-HMAC-SHA256: the file is public, so every
# guess at the password should cost an attacker this much work.
ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12


class SealError(Exception):
    """The credentials are missing, or do not open the envelope."""


def credentials_from_env() -> tuple[str, str]:
    user = os.environ.get("DASHBOARD_USERNAME") or ""
    password = os.environ.get("DASHBOARD_PASSWORD") or ""
    if not user or not password:
        raise SealError("DASHBOARD_USERNAME and DASHBOARD_PASSWORD must both be set")
    return user, password


def _key(user: str, password: str, salt: bytes, iterations: int) -> bytes:
    secret = f"{user}\n{password}".encode()
    return hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, dklen=32)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _salt(user: str) -> bytes:
    # Fixed per site and user, not random per build: the page keeps the derived
    # key for the session, and a new salt every deploy would sign everyone out.
    return hashlib.sha256(f"vllm-perf-eval:{user}".encode()).digest()[:SALT_BYTES]


def seal(payload: bytes, user: str, password: str) -> dict:
    """The payload gzipped and encrypted, as a JSON-ready envelope."""
    if not user or not password:
        raise SealError("both a username and a password are required")
    salt, iv = _salt(user), secrets.token_bytes(IV_BYTES)
    # Compressed first: encrypted bytes do not compress.
    data = AESGCM(_key(user, password, salt, ITERATIONS)).encrypt(iv, gzip.compress(payload), None)
    return {
        "v": VERSION,
        "kdf": KDF,
        "iter": ITERATIONS,
        "salt": _b64(salt),
        "iv": _b64(iv),
        "data": _b64(data),
    }


def unseal(envelope: dict, user: str, password: str) -> bytes:
    """The payload inside an envelope; SealError if these credentials do not open it."""
    if envelope.get("v") != VERSION or envelope.get("kdf") != KDF:
        raise SealError(f"unknown envelope version {envelope.get('v')!r}")
    try:
        salt, iv, data = (base64.b64decode(envelope[k]) for k in ("salt", "iv", "data"))
        key = _key(user, password, salt, int(envelope["iter"]))
        return gzip.decompress(AESGCM(key).decrypt(iv, data, None))
    except (InvalidTag, KeyError, ValueError) as exc:
        raise SealError("wrong username or password, or a damaged file") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--open", type=Path, required=True, help="Sealed payload to decrypt")
    parser.add_argument("--output", type=Path, required=True, help="Where to write the payload")
    args = parser.parse_args()

    # For the change check: a live payload that is missing or will not open
    # writes nothing, which payload_changed.py counts as changed.
    try:
        envelope = json.loads(args.open.read_text(encoding="utf-8"))
        args.output.write_bytes(unseal(envelope, *credentials_from_env()))
    except (OSError, ValueError, SealError) as exc:
        print(f"Could not open {args.open}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
