import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import verify_export
import verify_sonnet_contest as sonnet


REFEREE_PRIVATE = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def base58btc_encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = verify_export.BASE58_ALPHABET[remainder] + encoded
    return encoded


def referee_did() -> str:
    raw = verify_export.MULTICODEC_ED25519 + REFEREE_PRIVATE.public_key().public_bytes_raw()
    return "did:key:z" + base58btc_encode(raw)


def signed_record(seq: int, message: dict) -> dict:
    text = json.dumps(message, sort_keys=True, separators=(",", ":"))
    signature = REFEREE_PRIVATE.sign(
        f"{sonnet.RULES_ROOM}|{seq}|{text}".encode()
    )
    return {
        "seq": seq,
        "ts": "2026-09-14T00:00:00Z",
        "from": sonnet.REFEREE_DID,
        "text": text,
        "nonce": seq,
        "sig": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def launch() -> dict:
    return {
        "type": "sonnet.launch.v1",
        "status": "open",
        "configuration": {
            "contest_id": sonnet.CONTEST_ID,
            "referee": sonnet.REFEREE_DID,
            "deadline": sonnet.DEADLINE,
            "rooms": {"rules": sonnet.RULES_ROOM},
            "package_fingerprint": {"manifest_sha256": sonnet.MANIFEST_SHA256},
        },
        "package": {"url": sonnet.MANIFEST_URL, "sha256": sonnet.MANIFEST_SHA256},
    }


def status() -> dict:
    return {
        "type": "sonnet.notice.v1",
        "subject": "referee status",
        "contest_id": sonnet.CONTEST_ID,
        "referee": sonnet.REFEREE_DID,
        "participants": {"voter": 2, "writer": 1},
        "counts": {"handled": 10, "accepted": 8},
        "uptime_seconds": 100,
    }


def use_test_referee(monkeypatch):
    monkeypatch.setattr(sonnet, "REFEREE_DID", referee_did())


def test_valid_launch_and_latest_status(monkeypatch):
    use_test_referee(monkeypatch)
    found_launch, found_status = sonnet.inspect_records(
        [signed_record(1, launch()), signed_record(2, status()), signed_record(3, status())]
    )
    assert found_launch["seq"] == 1
    assert found_status["seq"] == 3


def test_status_history_detects_restart_and_count_reset(monkeypatch):
    use_test_referee(monkeypatch)
    before = status()
    after = status()
    after["uptime_seconds"] = 5
    after["counts"] = {"handled": 2, "accepted": 1}
    history = sonnet.analyze_status_history(
        [signed_record(1, launch()), signed_record(2, before), signed_record(3, after)]
    )
    assert history == {
        "verified_statuses": 2,
        "restart_sequences": [3],
        "count_reset_sequences": [3],
    }


def test_status_history_ignores_untrusted_and_invalid_counters(monkeypatch):
    use_test_referee(monkeypatch)
    first = status()
    second = status()
    first["uptime_seconds"] = True
    second["uptime_seconds"] = 1
    first["counts"] = {"handled": True}
    second["counts"] = {"handled": 0}
    forged = signed_record(4, status())
    forged["from"] = "did:key:z6MkjjzKLw96nMncMPEnXhhxeFkpHzN3pq2MDD8oMauHFnsn"
    history = sonnet.analyze_status_history(
        [signed_record(1, launch()), signed_record(2, first), signed_record(3, second), forged]
    )
    assert history["verified_statuses"] == 2
    assert history["restart_sequences"] == []
    assert history["count_reset_sequences"] == []


def test_forged_referee_is_ignored(monkeypatch):
    use_test_referee(monkeypatch)
    forged = signed_record(2, status())
    forged["from"] = "did:key:z6MkjjzKLw96nMncMPEnXhhxeFkpHzN3pq2MDD8oMauHFnsn"
    _, found_status = sonnet.inspect_records([signed_record(1, launch()), forged])
    assert found_status is None


def test_changed_deadline_is_rejected(monkeypatch):
    use_test_referee(monkeypatch)
    message = launch()
    message["configuration"]["deadline"] += 1
    try:
        sonnet.inspect_records([signed_record(1, message)])
    except verify_export.VerificationError:
        return
    raise AssertionError("changed deadline must be rejected")


def test_changed_signed_text_is_rejected(monkeypatch):
    use_test_referee(monkeypatch)
    record = signed_record(1, launch())
    record["text"] += " "
    try:
        sonnet.inspect_records([record])
    except verify_export.VerificationError:
        return
    raise AssertionError("changed signed text must be rejected")


def test_pinned_manifest_digest_matches_downloaded_bytes():
    body = sonnet.download_manifest()
    assert hashlib.sha256(body).hexdigest() == sonnet.MANIFEST_SHA256
