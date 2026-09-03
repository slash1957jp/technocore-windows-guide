# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=45,<48"]
# ///
"""Read-only preflight scanner for signed tclk/1 offers on Technocore.

The scanner verifies the Technocore Ed25519 transport signature, the frame sender,
canonical JSON encoding, the offer id, deadlines, and the documented field shapes.
It never signs, posts, reads an identity file, or checks/moves funds on a settlement rail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import verify_export


OFFER_PREFIX = "tclk1 "
OFFER_ROOM = "tclk-offers"
OFFER_REQUIRED = {
    "type", "from", "role", "amount", "asset", "lock", "rails",
    "claimByMs", "refundAfterMs", "expiresMs", "nonce", "id",
}
OFFER_ALLOWED = OFFER_REQUIRED | {"paymentKey", "job"}
CANONICAL_RAILS = {
    "btc-htlc", "evm-htlc", "flop-htlc", "memory", "near-htlc", "paper", "x402"
}
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
HEX32_RE = re.compile(r"0x[0-9a-f]{64}")
HEX33_RE = re.compile(r"0x[0-9a-f]{66}")
FRAME_NONCE_RE = re.compile(r"[0-9a-f]{8,64}")
AMOUNT_RE = re.compile(r"[1-9][0-9]*")
ASSET_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class OfferError(ValueError):
    """A record is not a valid current tclk/1 offer."""


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def expected_offer_id(offer: dict[str, Any]) -> str:
    core = dict(offer)
    core.pop("id", None)
    payload = "FLOP::tclk::v1|offer|" + canonical_json(core)
    return "0x" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OfferError(f"{name} must be a positive integer")
    return value


def validate_offer_record(record: dict[str, Any]) -> dict[str, Any]:
    """Authenticate and validate one complete Technocore record as an offer."""
    verify_export.verify_record(OFFER_ROOM, record)
    text = record.get("text")
    if not isinstance(text, str) or not text.startswith(OFFER_PREFIX):
        raise OfferError("record is not a tclk1 frame")
    try:
        offer = json.loads(text[len(OFFER_PREFIX):])
    except json.JSONDecodeError as exc:
        raise OfferError("tclk1 payload is not JSON") from exc
    if not isinstance(offer, dict):
        raise OfferError("tclk1 payload must be an object")
    if offer.get("type") != "offer":
        raise OfferError("frame is not an offer")
    missing = OFFER_REQUIRED - offer.keys()
    unknown = offer.keys() - OFFER_ALLOWED
    if missing:
        raise OfferError(f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise OfferError(f"unknown fields: {', '.join(sorted(unknown))}")
    if offer.get("from") != record.get("from"):
        raise OfferError("frame.from does not match the signed transport sender")
    if not isinstance(offer["from"], str) or not DID_RE.fullmatch(offer["from"]):
        raise OfferError("from is not an Ed25519 did:key")
    if offer["role"] not in {"payer", "payee"}:
        raise OfferError("role must be payer or payee")
    if not isinstance(offer["amount"], str) or not AMOUNT_RE.fullmatch(offer["amount"]):
        raise OfferError("amount must be a positive decimal integer string")
    if not isinstance(offer["asset"], str) or not ASSET_RE.fullmatch(offer["asset"]):
        raise OfferError("asset has an invalid shape")
    if offer["lock"] not in {"hash", "point"}:
        raise OfferError("lock must be hash or point")
    rails = offer["rails"]
    if not isinstance(rails, list) or not rails or any(
        not isinstance(rail, str) or rail not in CANONICAL_RAILS for rail in rails
    ):
        raise OfferError("rails must contain canonical registered rail ids")
    if len(set(rails)) != len(rails) or rails != sorted(rails):
        raise OfferError("rails must be unique and lexically sorted")
    if offer["lock"] == "point" and (
        not isinstance(offer.get("paymentKey"), str)
        or not HEX33_RE.fullmatch(offer["paymentKey"])
    ):
        raise OfferError("point locks require a compressed secp256k1 paymentKey")
    if "paymentKey" in offer and (
        not isinstance(offer["paymentKey"], str)
        or not HEX33_RE.fullmatch(offer["paymentKey"])
    ):
        raise OfferError("paymentKey has an invalid shape")
    if not isinstance(offer["nonce"], str) or not FRAME_NONCE_RE.fullmatch(offer["nonce"]):
        raise OfferError("nonce has an invalid shape")
    if not isinstance(offer["id"], str) or not HEX32_RE.fullmatch(offer["id"]):
        raise OfferError("id has an invalid shape")
    claim_by = _positive_int(offer["claimByMs"], "claimByMs")
    refund_after = _positive_int(offer["refundAfterMs"], "refundAfterMs")
    _positive_int(offer["expiresMs"], "expiresMs")
    if claim_by >= refund_after:
        raise OfferError("claimByMs must be earlier than refundAfterMs")
    job = offer.get("job")
    if job is not None:
        if not isinstance(job, dict) or set(job) - {"proto", "id", "context"}:
            raise OfferError("job has an invalid shape")
        if not all(isinstance(job.get(key), str) and job[key] for key in ("proto", "id")):
            raise OfferError("job requires non-empty proto and id strings")
        if "context" in job and (not isinstance(job["context"], str) or not job["context"]):
            raise OfferError("job.context must be a non-empty string")
    if text != OFFER_PREFIX + canonical_json(offer):
        raise OfferError("frame is not canonical ASCII JSON")
    if offer["id"] != expected_offer_id(offer):
        raise OfferError("offer id does not match the canonical offer")
    return offer


def rail_label(rails: list[str]) -> str:
    if set(rails) <= {"paper", "memory"}:
        return "REHEARSAL_ONLY (no value)"
    return "EXTERNAL_RAIL_UNVERIFIED"


def parse_timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify and summarize signed canonical offers in tclk-offers."
    )
    parser.add_argument("--file", type=Path, help="scan a saved tclk-offers JSONL export")
    parser.add_argument("--base-url", default="https://technocore.chat")
    parser.add_argument("--limit", type=int, default=20, help="maximum offers to print")
    parser.add_argument("--include-expired", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or args.limit > 200:
        print("ERROR: --limit must be between 1 and 200", file=sys.stderr)
        return 2
    try:
        raw = args.file.read_bytes() if args.file else verify_export.download_export(
            args.base_url, OFFER_ROOM
        )[0]
        records = verify_export.load_records(raw)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    valid: list[tuple[dict[str, Any], dict[str, Any]]] = []
    invalid = 0
    other_frames = 0
    for record in records:
        record.pop("_line_number", None)
        text = record.get("text")
        if not isinstance(text, str) or not text.startswith(OFFER_PREFIX):
            continue
        try:
            frame_type = json.loads(text[len(OFFER_PREFIX):]).get("type")
        except (json.JSONDecodeError, AttributeError):
            invalid += 1
            continue
        if frame_type != "offer":
            other_frames += 1
            continue
        try:
            offer = validate_offer_record(record)
        except (TypeError, ValueError, verify_export.VerificationError):
            invalid += 1
            continue
        valid.append((record, offer))

    shown = 0
    for record, offer in reversed(valid):
        active = offer["expiresMs"] > now_ms
        if not active and not args.include_expired:
            continue
        job = offer.get("job") or {}
        print(
            f"seq={record.get('seq')} status={'active' if active else 'expired'} "
            f"asset={offer['asset']} amount={offer['amount']} role={offer['role']} "
            f"rails={','.join(offer['rails'])} safety={rail_label(offer['rails'])}"
        )
        print(f"  from={offer['from']} id={offer['id']}")
        if job:
            print(f"  job={job['proto']}:{job['id']}")
        shown += 1
        if shown >= args.limit:
            break

    print(
        f"summary records={len(records)} valid_offers={len(valid)} "
        f"rejected_offer_frames={invalid} other_tclk_frames={other_frames} shown={shown}"
    )
    print("NOTE: a valid signature proves authorship only; it does not verify funds or work.")
    # Rejected frames are expected on a world-writable room and are data, not a
    # scanner failure. Operational failures above still return 2.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
