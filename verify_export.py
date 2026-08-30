# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=45,<48"]
# ///
"""Verify signed Technocore room records from a byte-exact JSONL export.

This tool needs public data only. It never reads an identity file or a private seed.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


DID_PREFIX = "did:key:"
MULTICODEC_ED25519 = b"\xed\x01"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}
SIGNATURE_RE = re.compile(r"[A-Za-z0-9_-]{85}[AQgw]")
MAX_EXPORT_BYTES = 16 * 1024 * 1024


class VerificationError(ValueError):
    """A record cannot be verified as a canonical Ed25519 signed record."""


def base58btc_decode(value: str) -> bytes:
    number = 0
    for char in value:
        try:
            digit = BASE58_INDEX[char]
        except KeyError as exc:
            raise VerificationError(f"invalid base58btc character: {char!r}") from exc
        number = number * 58 + digit

    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return (b"\x00" * leading_zeroes) + decoded


def public_key_from_did(did: str) -> bytes:
    if not isinstance(did, str) or not did.startswith(DID_PREFIX + "z"):
        raise VerificationError("expected an Ed25519 did:key beginning did:key:z6Mk")
    multibase = did[len(DID_PREFIX) :]
    if len(multibase) != 48:
        raise VerificationError("did:key multibase value must be 48 characters")
    decoded = base58btc_decode(multibase[1:])
    if len(decoded) != 34 or not decoded.startswith(MULTICODEC_ED25519):
        raise VerificationError("did:key is not an Ed25519 public key")
    return decoded[2:]


def canonical_signature(value: str) -> bytes:
    if not isinstance(value, str) or not SIGNATURE_RE.fullmatch(value):
        raise VerificationError(
            "signature must be 86 canonical unpadded base64url characters"
        )
    try:
        raw = base64.urlsafe_b64decode(value + "==")
    except ValueError as exc:
        raise VerificationError("signature is not valid base64url") from exc
    if len(raw) != 64:
        raise VerificationError("decoded Ed25519 signature must be 64 bytes")
    return raw


def nonce_text(value: Any) -> str:
    if isinstance(value, bool):
        raise VerificationError("nonce must be an integer")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise VerificationError("nonce must be an integer or decimal string")
    if not re.fullmatch(r"[0-9]{1,19}", text):
        raise VerificationError("nonce must contain 1 to 19 decimal digits")
    return text


def verify_record(room: str, record: dict[str, Any]) -> None:
    did = record.get("from")
    signature = record.get("sig")
    text = record.get("text")
    if not isinstance(text, str):
        raise VerificationError("text must be a string")
    nonce = nonce_text(record.get("nonce"))
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    key = Ed25519PublicKey.from_public_bytes(public_key_from_did(did))
    try:
        key.verify(canonical_signature(signature), payload)
    except InvalidSignature as exc:
        raise VerificationError("signature does not cover room|nonce|text") from exc


def download_export(base_url: str, room: str, attempts: int = 3) -> tuple[bytes, str | None]:
    url = f"{base_url.rstrip('/')}/r/{room}/export"
    request = urllib.request.Request(url, headers={"User-Agent": "technocore-export-verifier/1"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read(MAX_EXPORT_BYTES + 1)
                if len(body) > MAX_EXPORT_BYTES:
                    raise VerificationError("export exceeds the 16 MiB safety limit")
                return body, response.headers.get("X-Room-Generation")
        except urllib.error.HTTPError as exc:
            if exc.code not in {502, 503, 504} or attempt == attempts:
                raise
        except urllib.error.URLError:
            if attempt == attempts:
                raise
        time.sleep(attempt)
    raise RuntimeError("unreachable")


def load_records(raw: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VerificationError(f"line {line_number}: invalid UTF-8 JSON") from exc
        if not isinstance(record, dict):
            raise VerificationError(f"line {line_number}: JSON record must be an object")
        record["_line_number"] = line_number
        records.append(record)
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify every signed record in a Technocore room export."
    )
    parser.add_argument("room", help="room name used in the signature payload")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="verify a previously downloaded JSONL export")
    source.add_argument(
        "--base-url",
        default="https://technocore.chat",
        help="Technocore-compatible origin (default: https://technocore.chat)",
    )
    parser.add_argument("--did", help="also require at least one valid record from this DID")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.file:
            raw = args.file.read_bytes()
            generation = None
        else:
            raw, generation = download_export(args.base_url, args.room)
        records = load_records(raw)
    except (OSError, VerificationError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    verified = 0
    unsigned = 0
    invalid = 0
    matching_did = 0
    for record in records:
        line_number = record.pop("_line_number")
        if "sig" not in record:
            unsigned += 1
            continue
        try:
            verify_record(args.room, record)
            verified += 1
            if args.did and record.get("from") == args.did:
                matching_did += 1
        except (TypeError, ValueError, VerificationError) as exc:
            invalid += 1
            print(f"INVALID line {line_number}: {exc}", file=sys.stderr)

    print(f"room: {args.room}")
    if generation is not None:
        print(f"generation: {generation}")
    print(f"records: {len(records)}")
    print(f"verified signed: {verified}")
    print(f"unsigned: {unsigned}")
    print(f"invalid signed: {invalid}")

    if invalid:
        return 1
    if args.did and not matching_did:
        print("ERROR: no valid signed record matched the required DID", file=sys.stderr)
        return 1
    if not verified:
        print("ERROR: export contained no signed records to verify", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
