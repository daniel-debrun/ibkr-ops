"""Verification of signed order inputs (ed25519).

A signed input is a data file ``<data>`` accompanied by ``<data>.sig.json``, a manifest containing at least
``sha256`` (of the data file), ``public_key`` (hex), ``expires_at`` (ISO 8601 with UTC offset) and
``signature`` (hex, over the canonical JSON of all other fields). Additional fields are covered by the
signature.

Trust is anchored to the public key (or its SHA-256 fingerprint). Key identifiers carried inside a manifest
are not trusted, since any signer can assert them. ``extra_checks`` allows application-specific rules.

Requires PyNaCl (``pip install ibkr-ops[signing]``).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable

SUFFIX = ".sig.json"


class Refused(Exception):
    pass


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def key_fingerprint(public_key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()


def sign(data_path: str | Path, signing_key_hex: str, expires_at: dt.datetime, **extra) -> Path:
    """Write ``<data>.sig.json`` for ``data_path`` (used by tests and by producers)."""
    from nacl.encoding import HexEncoder
    from nacl.signing import SigningKey

    from .io import atomic_write_text

    data_path = Path(data_path)
    sk = SigningKey(signing_key_hex, encoder=HexEncoder)
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must be timezone-aware")
    meta = {"sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
            "public_key": sk.verify_key.encode(encoder=HexEncoder).decode(),
            "expires_at": expires_at.isoformat(), **extra}
    meta["signature"] = sk.sign(canonical(meta)).signature.hex()
    return atomic_write_text(data_path.with_name(data_path.name + SUFFIX), json.dumps(meta, indent=1))


def verify(sig_path: str | Path, trusted: Iterable[str], now: dt.datetime | None = None,
           extra_checks: Iterable[Callable[[dict], str | None]] = ()) -> tuple[Path, dict]:
    """``(data file, manifest)`` if every rule passes, else ``Refused`` with the first failing reason.

    ``trusted`` holds public keys (hex) or their sha256 fingerprints."""
    from nacl.encoding import HexEncoder
    from nacl.exceptions import BadSignatureError
    from nacl.signing import VerifyKey

    sig_path = Path(sig_path)
    if not sig_path.name.endswith(SUFFIX):
        raise Refused(f"{sig_path.name} is not a signed output (*{SUFFIX})")
    try:
        meta = json.loads(sig_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Refused(f"unreadable manifest: {e}") from None
    sig = meta.pop("signature", "")
    pub = str(meta.get("public_key", ""))
    try:
        VerifyKey(pub, encoder=HexEncoder).verify(canonical(meta), bytes.fromhex(sig))
    except (BadSignatureError, ValueError, TypeError):
        raise Refused("the signature does not match the manifest") from None
    trusted = {t.lower() for t in trusted}
    if pub.lower() not in trusted and key_fingerprint(pub) not in trusted:
        raise Refused(f"public key {key_fingerprint(pub)[:16]}... is not trusted")
    data = sig_path.with_name(sig_path.name[: -len(SUFFIX)])
    if not data.is_file():
        raise Refused(f"the signed file {data.name} is missing")
    if hashlib.sha256(data.read_bytes()).hexdigest() != meta.get("sha256"):
        raise Refused(f"{data.name} was changed after it was signed")
    try:
        exp = dt.datetime.fromisoformat(meta["expires_at"])
    except (KeyError, ValueError):
        raise Refused("the manifest has no valid expires_at") from None
    if exp.tzinfo is None:
        raise Refused("expires_at has no UTC offset")
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.astimezone()
    if exp < now:
        raise Refused(f"the output expired at {meta['expires_at']}")
    for chk in extra_checks:
        why = chk(meta)
        if why:
            raise Refused(why)
    return data, meta
