import base64
import json
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


def signed_offer(
    *,
    frame_from_mismatch: bool = False,
    bad_id: bool = False,
    amount: str = "1",
    missing: set[str] | None = None,
    unknown_field: bool = False,
    noncanonical: bool = False,
):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    did = "did:key:z" + base58btc_encode(verify_export.MULTICODEC_ED25519 + public)
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    offer = {
        "amount": amount,
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
    for key in missing or set():
        offer.pop(key, None)
    if unknown_field:
        offer["description"] = "not part of tclk/1"
    if bad_id:
        offer["id"] = "0x" + "00" * 32
    if frame_from_mismatch:
        offer["from"] = "did:key:z6Mk" + "1" * 44
    payload = (
        json.dumps(dict(reversed(list(offer.items()))), separators=(",", ":"))
        if noncanonical
        else scan_tclk_offers.canonical_json(offer)
    )
    text = scan_tclk_offers.OFFER_PREFIX + payload
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


def signed_accept(*, missing_contract: bool = False, noncanonical: bool = False):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    did = "did:key:z" + base58btc_encode(verify_export.MULTICODEC_ED25519 + public)
    accept = {
        "contract": "0x" + "11" * 32,
        "from": did,
        "nonce": "0123456789abcdef",
        "ref": "0x" + "22" * 32,
        "statement": "0x" + "33" * 32,
        "type": "accept",
    }
    if missing_contract:
        accept.pop("contract")
    payload = (
        json.dumps(dict(reversed(list(accept.items()))), separators=(",", ":"))
        if noncanonical
        else scan_tclk_offers.canonical_json(accept)
    )
    text = scan_tclk_offers.OFFER_PREFIX + payload
    nonce = 8
    sig = private.sign(f"tclk-offers|{nonce}|{text}".encode())
    return {
        "seq": 2,
        "ts": "2026-09-10T00:00:00Z",
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


def test_valid_signed_accept_is_accepted():
    accept = scan_tclk_offers.validate_accept_record(signed_accept())
    assert accept["contract"] == "0x" + "11" * 32


def test_accept_without_contract_is_rejected():
    try:
        scan_tclk_offers.validate_accept_record(signed_accept(missing_contract=True))
    except scan_tclk_offers.OfferError as exc:
        assert "contract" in str(exc)
        return
    raise AssertionError("an accept without contract must be rejected")


def test_noncanonical_accept_is_rejected():
    try:
        scan_tclk_offers.validate_accept_record(signed_accept(noncanonical=True))
    except scan_tclk_offers.OfferError as exc:
        assert "canonical" in str(exc)
        return
    raise AssertionError("an accept with insertion-order JSON must be rejected")


def test_incomplete_offer_shape_is_classified():
    record = signed_offer(missing={"from", "id", "role", "lock"})
    try:
        scan_tclk_offers.validate_offer_record(record)
    except scan_tclk_offers.OfferError as exc:
        assert scan_tclk_offers.classify_offer_rejection(exc) == "incomplete_shape"
        return
    raise AssertionError("an incomplete offer-shaped payload must be rejected")


def test_unknown_offer_fields_are_classified():
    try:
        scan_tclk_offers.validate_offer_record(signed_offer(unknown_field=True))
    except scan_tclk_offers.OfferError as exc:
        assert scan_tclk_offers.classify_offer_rejection(exc) == "unknown_fields"
        return
    raise AssertionError("unknown offer fields must be rejected")


def test_decimal_amount_is_classified():
    try:
        scan_tclk_offers.validate_offer_record(signed_offer(amount="20.0"))
    except scan_tclk_offers.OfferError as exc:
        assert scan_tclk_offers.classify_offer_rejection(exc) == "bad_amount"
        return
    raise AssertionError("a decimal-form amount must be rejected")


def test_noncanonical_offer_is_classified():
    try:
        scan_tclk_offers.validate_offer_record(signed_offer(noncanonical=True))
    except scan_tclk_offers.OfferError as exc:
        assert scan_tclk_offers.classify_offer_rejection(exc) == "noncanonical"
        return
    raise AssertionError("a non-canonical offer must be rejected")
