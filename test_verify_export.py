import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import verify_export


def base58btc_encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = verify_export.BASE58_ALPHABET[remainder] + encoded
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeroes + encoded


def signed_record(room: str, nonce: int, text: str) -> dict:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    did = "did:key:z" + base58btc_encode(verify_export.MULTICODEC_ED25519 + public)
    signature = private.sign(f"{room}|{nonce}|{text}".encode())
    return {
        "seq": 1,
        "ts": "2026-08-30T00:00:00Z",
        "from": did,
        "sig": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "nonce": nonce,
        "text": text,
    }


def test_valid_record_verifies():
    record = signed_record("proofs", 3, "attributable claim")
    verify_export.verify_record("proofs", record)


def test_changed_text_is_rejected():
    record = signed_record("proofs", 3, "attributable claim")
    record["text"] = "changed"
    try:
        verify_export.verify_record("proofs", record)
    except verify_export.VerificationError:
        return
    raise AssertionError("changed text must not verify")


def test_jsonl_loader_keeps_large_nonce_exact():
    record = signed_record("proofs", 9_223_372_036_854_775_807, "large nonce")
    raw = (json.dumps(record) + "\n").encode()
    loaded = verify_export.load_records(raw)[0]
    loaded.pop("_line_number")
    verify_export.verify_record("proofs", loaded)
