# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=45,<48"]
# ///
"""Verify the official Sonnet Challenge launch and latest referee status.

This tool uses public data only. It never reads an identity file, signs a
message, registers a participant, votes, or handles a wallet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verify_export import VerificationError, download_export, load_records, verify_record


RULES_ROOM = "d-sonnet-2-rules"
CONTEST_ID = "sonnet-2"
REFEREE_DID = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
OPENING = 1_789_128_000.0
IDENTITY_CUTOFF = 1_789_128_000.0
DEADLINE = 1_789_732_800.0
POEM_PRIZE = 50_000
VOTER_POOL = 50_000
RULES_VERSION = "0.5"
MANIFEST_SHA256 = "0c87c41b8b33bdd8641f77c9e481a12f2758a0e27d47b90452b1c0a2020a9547"
MANIFEST_URL = (
    "https://raw.githubusercontent.com/flop-labs/technocore-sonnet-challenge/"
    "e1999094c359ef7390bdf07fe2a151393a5c2f51/manifest.json"
)
MAX_MANIFEST_BYTES = 1024 * 1024


def _nonnegative_int(value: Any) -> int | None:
    """Return a JSON integer but reject bools and negative counters."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def parse_signed_json(record: dict[str, Any]) -> dict[str, Any]:
    """Verify a referee record before interpreting its JSON text."""
    if record.get("from") != REFEREE_DID:
        raise VerificationError("record is not from the pinned referee DID")
    verify_record(RULES_ROOM, record)
    try:
        message = json.loads(record["text"])
    except json.JSONDecodeError as exc:
        raise VerificationError("signed referee text is not JSON") from exc
    if not isinstance(message, dict):
        raise VerificationError("signed referee JSON must be an object")
    return message


def validate_launch(message: dict[str, Any]) -> None:
    """Require the launch values pinned by the official LAUNCH.md record."""
    configuration = message.get("configuration")
    package = message.get("package")
    if message.get("type") != "sonnet.launch.v1":
        raise VerificationError("expected sonnet.launch.v1")
    if message.get("status") != "open":
        raise VerificationError("pinned launch is not open")
    if not isinstance(configuration, dict) or not isinstance(package, dict):
        raise VerificationError("launch lacks configuration or package")
    expected = {
        "contest_id": CONTEST_ID,
        "referee": REFEREE_DID,
        "opening": OPENING,
        "identity_cutoff": IDENTITY_CUTOFF,
        "deadline": DEADLINE,
        "prize": POEM_PRIZE,
        "voter_pool": VOTER_POOL,
        "rules_version": RULES_VERSION,
    }
    for key, value in expected.items():
        if configuration.get(key) != value:
            raise VerificationError(f"unexpected launch configuration: {key}")
    rooms = configuration.get("rooms")
    if not isinstance(rooms, dict) or rooms.get("rules") != RULES_ROOM:
        raise VerificationError("launch points to an unexpected rules room")
    fingerprint = configuration.get("package_fingerprint")
    if not isinstance(fingerprint, dict) or fingerprint.get("manifest_sha256") != MANIFEST_SHA256:
        raise VerificationError("launch manifest fingerprint changed")
    if package.get("url") != MANIFEST_URL or package.get("sha256") != MANIFEST_SHA256:
        raise VerificationError("launch package URL or digest changed")


def inspect_records(records: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    launches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    notices: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for original in records:
        record = {key: value for key, value in original.items() if key != "_line_number"}
        if record.get("from") != REFEREE_DID or "sig" not in record:
            continue
        message = parse_signed_json(record)
        configuration = message.get("configuration")
        nested_contest_id = configuration.get("contest_id") if isinstance(configuration, dict) else None
        if message.get("contest_id", nested_contest_id) != CONTEST_ID:
            continue
        if message.get("type") == "sonnet.launch.v1":
            launches.append((record, message))
        elif message.get("type") == "sonnet.notice.v1" and message.get("subject") == "referee status":
            if message.get("referee") != REFEREE_DID:
                raise VerificationError("status message names an unexpected referee")
            notices.append((record, message))

    if len(launches) != 1:
        raise VerificationError(f"expected exactly one valid official launch, found {len(launches)}")
    launch_record, launch = launches[0]
    validate_launch(launch)
    latest_notice = max(notices, key=lambda item: item[0].get("seq", -1), default=None)
    return launch_record, latest_notice[0] if latest_notice else None


def analyze_status_history(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe signed status continuity without treating window counts as totals.

    Referee process restarts are visible when ``uptime_seconds`` decreases.  The
    ``counts`` object can reset with the process, whereas ``participants`` has
    so far remained durable.  Reporting the distinction avoids presenting a
    per-process counter as a lifetime contest total.
    """
    statuses: list[tuple[int, dict[str, Any]]] = []
    for original in records:
        record = {key: value for key, value in original.items() if key != "_line_number"}
        if record.get("from") != REFEREE_DID or "sig" not in record:
            continue
        message = parse_signed_json(record)
        if (
            message.get("type") == "sonnet.notice.v1"
            and message.get("subject") == "referee status"
            and message.get("contest_id") == CONTEST_ID
            and message.get("referee") == REFEREE_DID
        ):
            seq = _nonnegative_int(record.get("seq"))
            if seq is not None:
                statuses.append((seq, message))

    statuses.sort(key=lambda item: item[0])
    restart_sequences: list[int] = []
    count_reset_sequences: list[int] = []
    previous: dict[str, Any] | None = None
    for seq, current in statuses:
        if previous is not None:
            previous_uptime = _nonnegative_int(previous.get("uptime_seconds"))
            current_uptime = _nonnegative_int(current.get("uptime_seconds"))
            if (
                previous_uptime is not None
                and current_uptime is not None
                and current_uptime < previous_uptime
            ):
                restart_sequences.append(seq)

            previous_counts = previous.get("counts")
            current_counts = current.get("counts")
            if isinstance(previous_counts, dict) and isinstance(current_counts, dict):
                shared = previous_counts.keys() & current_counts.keys()
                if any(
                    (old := _nonnegative_int(previous_counts[key])) is not None
                    and (new := _nonnegative_int(current_counts[key])) is not None
                    and new < old
                    for key in shared
                ):
                    count_reset_sequences.append(seq)
        previous = current

    return {
        "verified_statuses": len(statuses),
        "restart_sequences": restart_sequences,
        "count_reset_sequences": count_reset_sequences,
    }


def download_manifest() -> bytes:
    request = urllib.request.Request(
        MANIFEST_URL, headers={"User-Agent": "sonnet-contest-verifier/1"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise VerificationError("manifest exceeds the 1 MiB safety limit")
    return body


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the pinned sonnet-2 launch and latest official referee status."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="use a saved rules-room JSONL export")
    source.add_argument(
        "--base-url",
        default="https://technocore.chat",
        help="Technocore-compatible origin (default: https://technocore.chat)",
    )
    parser.add_argument(
        "--skip-manifest",
        action="store_true",
        help="do not download and hash the pinned official manifest",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.file:
            raw = args.file.read_bytes()
            generation = None
        else:
            raw, generation = download_export(args.base_url, RULES_ROOM)
        records = load_records(raw)
        launch_record, notice_record = inspect_records(records)
        history = analyze_status_history(records)
        if not args.skip_manifest:
            digest = hashlib.sha256(download_manifest()).hexdigest()
            if digest != MANIFEST_SHA256:
                raise VerificationError("downloaded official manifest digest changed")
    except (OSError, VerificationError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("official launch: VERIFIED")
    print(f"contest: {CONTEST_ID}")
    print(f"referee: {REFEREE_DID}")
    print(f"launch sequence: {launch_record.get('seq')}")
    print(f"opening: {iso_utc(OPENING)}")
    print(f"identity cutoff: {iso_utc(IDENTITY_CUTOFF)}")
    print(f"deadline: {iso_utc(DEADLINE)}")
    print(f"poem prize: {POEM_PRIZE} FLOP")
    print(f"voter pool: {VOTER_POOL} FLOP")
    print(f"rules version: {RULES_VERSION}")
    print(f"manifest sha256: {MANIFEST_SHA256}")
    if generation is not None:
        print(f"room generation: {generation}")
    if notice_record is None:
        print("latest referee status: none retained")
    else:
        status = json.loads(notice_record["text"])
        print(
            "latest referee status: "
            f"VERIFIED sequence {notice_record.get('seq')} at {notice_record.get('ts')}"
        )
        participants = status.get("participants")
        if isinstance(participants, dict):
            summary = ", ".join(f"{key}={value}" for key, value in sorted(participants.items()))
            print(f"reported participants: {summary}")
        uptime = _nonnegative_int(status.get("uptime_seconds"))
        if uptime is not None:
            print(f"latest referee process uptime: {uptime} seconds")
        restarts = history["restart_sequences"]
        resets = history["count_reset_sequences"]
        if restarts:
            joined = ", ".join(str(seq) for seq in restarts)
            print(f"WARNING: referee restart detected before status sequence(s): {joined}")
        if resets:
            joined = ", ".join(str(seq) for seq in resets)
            print(
                "WARNING: status counts decreased at sequence(s): "
                f"{joined}; treat counts as process-window metrics, not lifetime totals"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
