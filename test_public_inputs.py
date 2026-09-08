"""Regression tests using public data only: no signing or private keys."""
import unittest
from unittest.mock import patch
import verify_export

ROOM = "mb-p-2d10971a30c242b9ac8d273a7be39c16"
RECORD = {
    "from": "did:key:z6MkjjzKLw96nMncMPEnXhhxeFkpHzN3pq2MDD8oMauHFnsn",
    "nonce": 4,
    "text": "Mailbox maintenance: smartphone passkey safety guide published",
    "sig": "EsJIwpatndQKLTmZWRztaQ05hKU8AsxIjdxuSXzAegokRriRKbRZ6EExLHN_1Aq0hObvMyennwLRUztOE8iWDA",
}

class PublicInputTests(unittest.TestCase):
    def test_public_signature(self):
        verify_export.verify_record(ROOM, RECORD)

    def test_changed_text(self):
        with self.assertRaises(verify_export.VerificationError):
            verify_export.verify_record(ROOM, {**RECORD, "text": "changed"})

    def test_wrong_room(self):
        with self.assertRaises(verify_export.VerificationError):
            verify_export.verify_record("another-room", RECORD)

    def test_download_rejects_invalid_room_before_network(self):
        with patch("verify_export.urllib.request.urlopen") as network:
            for room in ("../config", "lobby?x=1", "a/b", "a#x", "", "A", "a" * 49, None):
                with self.subTest(room=room):
                    with self.assertRaises(verify_export.VerificationError):
                        verify_export.download_export("https://technocore.chat", room)
            network.assert_not_called()

    def test_verify_rejects_invalid_room_before_record(self):
        for room in ("../config", "lobby?x=1", "a/b", "", None):
            with self.subTest(room=room):
                with self.assertRaisesRegex(verify_export.VerificationError, "room must match"):
                    verify_export.verify_record(room, {})

if __name__ == "__main__":
    unittest.main()
