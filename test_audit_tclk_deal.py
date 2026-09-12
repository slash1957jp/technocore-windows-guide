import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import audit_tclk_deal
import scan_tclk_offers
import verify_export

CONTRACT = "0x" + "11" * 32


def base58btc_encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = verify_export.BASE58_ALPHABET[remainder] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + encoded


def identity() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    did = "did:key:z" + base58btc_encode(verify_export.MULTICODEC_ED25519 + public)
    return private, did


def signed_record(
    private: Ed25519PrivateKey,
    did: str,
    room: str,
    frame: dict,
    *,
    seq: int = 1,
    nonce: int = 1,
    noncanonical: bool = False,
) -> dict:
    payload = (
        json.dumps(dict(reversed(list(frame.items()))), separators=(",", ":"))
        if noncanonical
        else scan_tclk_offers.canonical_json(frame)
    )
    text = scan_tclk_offers.OFFER_PREFIX + payload
    signature = private.sign(f"{room}|{nonce}|{text}".encode())
    return {
        "seq": seq,
        "ts": "2026-09-12T00:00:00Z",
        "from": did,
        "nonce": nonce,
        "sig": base64.urlsafe_b64encode(signature).decode().rstrip("="),
        "text": text,
    }


def lock_frame(did: str, contract: str = CONTRACT) -> dict:
    return {
        "contract": contract,
        "from": did,
        "rail": "paper",
        "ref": "paper-lock-1",
        "type": "lock",
    }


def test_deal_room_is_derived_from_contract_prefix():
    assert audit_tclk_deal.derive_deal_room(CONTRACT) == "mb-p-tclk-1111111111111111"


def test_party_signed_frame_is_accepted():
    private, did = identity()
    room = audit_tclk_deal.derive_deal_room(CONTRACT)
    record = signed_record(private, did, room, lock_frame(did))
    frame = audit_tclk_deal.audit_deal_record(record, room, CONTRACT, {did})
    assert frame["type"] == "lock"


def test_foreign_signed_frame_is_rejected():
    party_private, party_did = identity()
    foreign_private, foreign_did = identity()
    del party_private
    room = audit_tclk_deal.derive_deal_room(CONTRACT)
    record = signed_record(foreign_private, foreign_did, room, lock_frame(foreign_did))
    try:
        audit_tclk_deal.audit_deal_record(record, room, CONTRACT, {party_did})
    except audit_tclk_deal.DealAuditError as exc:
        assert "not a contract party" in str(exc)
        return
    raise AssertionError("a foreign sender must be rejected")


def test_frame_sender_must_match_transport_sender():
    private, did = identity()
    _, other_did = identity()
    room = audit_tclk_deal.derive_deal_room(CONTRACT)
    record = signed_record(private, did, room, lock_frame(other_did))
    try:
        audit_tclk_deal.audit_deal_record(record, room, CONTRACT, {did, other_did})
    except audit_tclk_deal.DealAuditError as exc:
        assert "transport sender" in str(exc)
        return
    raise AssertionError("a forged frame.from must be rejected")


def test_wrong_contract_is_rejected():
    private, did = identity()
    room = audit_tclk_deal.derive_deal_room(CONTRACT)
    record = signed_record(private, did, room, lock_frame(did, "0x" + "22" * 32))
    try:
        audit_tclk_deal.audit_deal_record(record, room, CONTRACT, {did})
    except audit_tclk_deal.DealAuditError as exc:
        assert "different contract" in str(exc)
        return
    raise AssertionError("a frame for another contract must be rejected")


def test_noncanonical_frame_is_rejected():
    private, did = identity()
    room = audit_tclk_deal.derive_deal_room(CONTRACT)
    record = signed_record(private, did, room, lock_frame(did), noncanonical=True)
    try:
        audit_tclk_deal.audit_deal_record(record, room, CONTRACT, {did})
    except audit_tclk_deal.DealAuditError as exc:
        assert "canonical" in str(exc)
        return
    raise AssertionError("a noncanonical frame must be rejected")


def test_party_roles_follow_offer_role():
    assert audit_tclk_deal.party_roles(
        {"from": "offerer", "role": "payer"}, {"from": "acceptor"}
    ) == ("offerer", "acceptor")
    assert audit_tclk_deal.party_roles(
        {"from": "offerer", "role": "payee"}, {"from": "acceptor"}
    ) == ("acceptor", "offerer")
