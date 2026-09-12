# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=45,<48"]
# ///
"""Read-only party/authorship audit for a retained tclk/1 deal room.

This tool authenticates the offer/accept handshake from ``tclk-offers``, derives
the contract room, and rejects deal-room records not signed by either party.  It
does not sign, post, read an identity file, or assert that a settlement rail paid.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import scan_tclk_offers
import verify_export

POST_ACCEPT_FIELDS = {
    "lock": (
        {"type", "from", "contract", "rail", "ref"},
        {"presig"},
    ),
    "reveal": (
        {"type", "from", "contract", "secret"},
        {"ref"},
    ),
    "refund": (
        {"type", "from", "contract"},
        {"ref", "reason"},
    ),
    "cancel": (
        {"type", "from", "contract"},
        {"reason"},
    ),
    "receipt": (
        {"type", "from", "contract", "outcome"},
        {"rail", "ref"},
    ),
    "heartbeat": (
        {"type", "from", "contract", "nonce"},
        {"note"},
    ),
}
CONTRACT_RE = re.compile(r"0x[0-9a-f]{64}")


class DealAuditError(ValueError):
    """A record cannot be trusted as this contract's party-authored data."""


def derive_deal_room(contract: str) -> str:
    if not isinstance(contract, str) or not CONTRACT_RE.fullmatch(contract):
        raise DealAuditError("contract must be 0x followed by 64 lowercase hex digits")
    return f"mb-p-tclk-{contract[2:18]}"


def _load(path: Path | None, base_url: str, room: str) -> list[dict[str, Any]]:
    raw = (
        path.read_bytes() if path else verify_export.download_export(base_url, room)[0]
    )
    return verify_export.load_records(raw)


def find_handshake(
    records: list[dict[str, Any]], contract: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Find a signed, ordered offer/accept pair for ``contract``."""
    offers: dict[str, dict[str, Any]] = {}
    for original in records:
        record = dict(original)
        record.pop("_line_number", None)
        text = record.get("text")
        if not isinstance(text, str) or not text.startswith(
            scan_tclk_offers.OFFER_PREFIX
        ):
            continue
        try:
            payload = json.loads(text[len(scan_tclk_offers.OFFER_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "offer":
            try:
                offer = scan_tclk_offers.validate_offer_record(record)
            except (TypeError, ValueError):
                continue
            offers.setdefault(offer["id"], offer)
            continue
        if payload.get("type") != "accept" or payload.get("contract") != contract:
            continue
        try:
            accept = scan_tclk_offers.validate_accept_record(record)
        except (TypeError, ValueError):
            continue
        offer = offers.get(accept["ref"])
        if offer is None:
            raise DealAuditError(
                "matching accept has no preceding retained valid offer"
            )
        try:
            scan_tclk_offers.validate_accept_against_offer(accept, offer)
        except scan_tclk_offers.OfferError as exc:
            raise DealAuditError(f"handshake rejected: {exc}") from exc
        return offer, accept
    raise DealAuditError("no retained authenticated handshake for this contract")


def party_roles(offer: dict[str, Any], accept: dict[str, Any]) -> tuple[str, str]:
    """Return ``(payer_did, payee_did)`` for an authenticated handshake."""
    if offer["role"] == "payer":
        return offer["from"], accept["from"]
    return accept["from"], offer["from"]


def audit_deal_record(
    record: dict[str, Any], room: str, contract: str, parties: set[str]
) -> dict[str, Any]:
    """Authenticate one deal-room record and require a contract party as sender."""
    try:
        verify_export.verify_record(room, record)
    except verify_export.VerificationError as exc:
        raise DealAuditError(f"transport signature: {exc}") from exc
    text = record.get("text")
    if not isinstance(text, str) or not text.startswith(scan_tclk_offers.OFFER_PREFIX):
        raise DealAuditError("record is not a tclk1 frame")
    try:
        frame = json.loads(text[len(scan_tclk_offers.OFFER_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise DealAuditError("tclk1 payload is not JSON") from exc
    if not isinstance(frame, dict):
        raise DealAuditError("tclk1 payload must be an object")
    frame_type = frame.get("type")
    if frame_type not in POST_ACCEPT_FIELDS:
        raise DealAuditError("frame type does not belong in a post-accept deal room")
    required, optional = POST_ACCEPT_FIELDS[frame_type]
    missing = required - frame.keys()
    unknown = frame.keys() - required - optional
    if missing:
        raise DealAuditError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise DealAuditError(f"unknown fields: {', '.join(sorted(unknown))}")
    if text != scan_tclk_offers.OFFER_PREFIX + scan_tclk_offers.canonical_json(frame):
        raise DealAuditError("frame is not canonical ASCII JSON")
    if frame.get("from") != record.get("from"):
        raise DealAuditError("frame.from does not match the signed transport sender")
    if frame["from"] not in parties:
        raise DealAuditError("signed sender is not a contract party")
    if frame.get("contract") != contract:
        raise DealAuditError("frame names a different contract")
    return frame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Authenticate a retained tclk/1 handshake and its deal-room senders."
    )
    parser.add_argument("contract", help="0x-prefixed tclk contract id")
    parser.add_argument(
        "--board-file", type=Path, help="saved tclk-offers JSONL export"
    )
    parser.add_argument("--deal-file", type=Path, help="saved deal-room JSONL export")
    parser.add_argument("--base-url", default="https://technocore.chat")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        room = derive_deal_room(args.contract)
        board_records = _load(
            args.board_file, args.base_url, scan_tclk_offers.OFFER_ROOM
        )
        offer, accept = find_handshake(board_records, args.contract)
        payer, payee = party_roles(offer, accept)
        deal_records = _load(args.deal_file, args.base_url, room)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    accepted = 0
    rejected = 0
    for original in deal_records:
        record = dict(original)
        record.pop("_line_number", None)
        try:
            frame = audit_deal_record(record, room, args.contract, {payer, payee})
        except DealAuditError as exc:
            rejected += 1
            print(f"REJECT seq={record.get('seq')} reason={exc}")
        else:
            accepted += 1
            print(
                f"ACCEPT seq={record.get('seq')} type={frame['type']} from={frame['from']}"
            )

    print(f"contract={args.contract}")
    print(f"room={room} payer={payer} payee={payee}")
    print(
        f"summary records={len(deal_records)} accepted_party_frames={accepted} rejected={rejected}"
    )
    print(
        "NOTE: this authenticates parties and frame placement only; it does not prove "
        "settlement, work delivery, or a valid full state transition."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
