import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import scan_tclk_offers
import verify_export


def base58btc_encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = verify_export.BASE58_ALPHABET[remainder] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + encoded


def signed_offer(*, frame_from_mismatch: bool = False, bad_id: bool = False):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    did = "did:key:z" + base58btc_encode(verify_export.MULTICODEC_ED25519 + public)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    offer = {
        "amount": "1",
        "asset": "PAPER",
        "claimByMs": now + 120_000,
        "expiresMs": now + 60_000,
        "from": did,
        "lock": "hash",
        "nonce": "0123456789abcdef",
        "rails": ["paper"],
        "refundAfterMs": now + 180_000,
        "role": "payer",
        "type": "offer",
    }
    offer["id"] = scan_tclk_offers.expected_offer_id(offer)
    if bad_id:
        offer["id"] = "0x" + "00" * 32
    if frame_from_mismatch:
        offer["from"] = "did:key:z6Mk" + "1" * 44
    text = scan_tclk_offers.OFFER_PREFIX + scan_tclk_offers.canonical_json(offer)
    nonce = 7
    sig = private.sign(f"tclk-offers|{nonce}|{text}".encode())
    return {
        "seq": 1,
        "ts": "2026-09-03T00:00:00Z",
        "from": did,
        "nonce": nonce,
        "sig": base64.urlsafe_b64encode(sig).decode().rstrip("="),
        "text": text,
    }


def test_valid_signed_offer_is_accepted():
    offer = scan_tclk_offers.validate_offer_record(signed_offer())
    assert offer["rails"] == ["paper"]
    assert scan_tclk_offers.rail_label(offer["rails"]) == "REHEARSAL_ONLY (no value)"


def test_frame_sender_must_match_transport_sender():
    try:
        scan_tclk_offers.validate_offer_record(signed_offer(frame_from_mismatch=True))
    except scan_tclk_offers.OfferError:
        return
    raise AssertionError("a different frame.from must be rejected")


def test_offer_id_is_recomputed():
    try:
        scan_tclk_offers.validate_offer_record(signed_offer(bad_id=True))
    except scan_tclk_offers.OfferError:
        return
    raise AssertionError("a forged offer id must be rejected")


def test_changed_record_text_breaks_transport_signature():
    record = signed_offer()
    record["text"] += " "
    try:
        scan_tclk_offers.validate_offer_record(record)
    except verify_export.VerificationError:
        return
    raise AssertionError("changed signed text must be rejected")
