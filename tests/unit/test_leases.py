"""Admission tests preserve the server's capacity and publisher ownership contracts."""
from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import uuid

from studio import Config, Leases


class Gateway:
    def __init__(self):
        self.paths = set()
        self.fail_remove = False
        self.fail_add_after_apply = False

    def __call__(self, route, data=None, method=None):
        if route.startswith("config/paths/add/"):
            self.paths.add(route.removeprefix("config/paths/add/"))
            if self.fail_add_after_apply:
                raise OSError("Gateway applied add but response was lost")
        elif route.startswith("config/paths/remove/"):
            if self.fail_remove:
                raise OSError("Gateway unavailable")
            self.paths.remove(route.removeprefix("config/paths/remove/"))
        return {}


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.gateway = Gateway()
        self.leases = Leases(Config(Path(self.directory.name), "http://localhost:8080"), self.gateway)

    def tearDown(self):
        self.leases.db.close()
        self.directory.cleanup()

    def reserve(self, client=None):
        try:
            return self.leases.reserve(client or uuid.uuid4().hex)
        except ValueError:
            return None

    def test_last_slot_race_has_one_winner(self):
        for _ in range(4):
            self.reserve()
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.reserve(), range(12)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.leases.rows()), 5)
        self.assertEqual(len(self.gateway.paths), 5)

    def test_concurrent_retries_reuse_one_lease(self):
        client = uuid.uuid4().hex
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.reserve(client), range(12)))
        self.assertEqual(len({result["lease_id"] for result in results}), 1)
        self.assertEqual(len(self.leases.rows()), 1)

    def test_release_denies_old_token_and_reuses_slot(self):
        old = self.reserve()
        self.assertTrue(self.leases.allow_publish(old["source_path"], old["token"]))
        self.leases.release(old["lease_id"])
        new = self.reserve()
        self.assertEqual(old["slot"], new["slot"])
        self.assertNotEqual(old["source_path"], new["source_path"])
        self.assertFalse(self.leases.allow_publish(old["source_path"], old["token"]))
        self.assertNotIn(old["source_path"], self.gateway.paths)

    def test_failed_fence_keeps_capacity_occupied(self):
        old = self.reserve()
        self.gateway.fail_remove = True
        with self.assertRaises(OSError):
            self.leases.release(old["lease_id"])
        self.assertEqual(self.leases.rows()[0]["state"], "REVOKING")
        self.assertFalse(self.leases.allow_publish(old["source_path"], old["token"]))
        self.assertEqual(len(self.leases.rows()), 1)
        self.gateway.fail_remove = False
        self.leases.release(old["lease_id"])
        self.assertEqual(len(self.leases.rows()), 0)

    def test_uncertain_add_holds_capacity_until_path_is_fenced(self):
        for _ in range(4):
            self.reserve()
        client = uuid.uuid4().hex
        self.gateway.fail_add_after_apply = True
        self.gateway.fail_remove = True
        with self.assertRaises(OSError):
            self.leases.reserve(client)
        uncertain = next(row for row in self.leases.rows() if row["client"] == client)
        token = self.leases.token(uncertain)
        self.assertEqual(uncertain["state"], "REVOKING")
        self.assertEqual(len(self.leases.rows()), 5)
        self.assertEqual(len(self.gateway.paths), 5)
        self.assertFalse(self.leases.allow_publish(uncertain["path"], token))
        self.gateway.fail_add_after_apply = False
        self.assertIsNone(self.reserve())
        with self.assertRaises(OSError):
            self.leases.release(uncertain["id"])
        self.assertEqual(len(self.leases.rows()), 5)
        self.assertIn(uncertain["path"], self.gateway.paths)
        self.gateway.fail_remove = False
        self.leases.release(uncertain["id"])
        self.assertNotIn(uncertain["path"], self.gateway.paths)
        self.assertEqual(len(self.leases.rows()), 4)
        replacement = self.reserve()
        self.assertEqual(replacement["slot"], uncertain["slot"])
        self.assertNotEqual(replacement["source_path"], uncertain["path"])
        self.assertFalse(self.leases.allow_publish(uncertain["path"], token))

    def test_ignored_permission_expires_reservation(self):
        with patch("studio.time.monotonic", return_value=100):
            lease = self.reserve()
        with patch("studio.time.monotonic", return_value=161):
            self.assertEqual(self.leases.expired(), [lease["lease_id"]])
            self.assertFalse(self.leases.allow_publish(lease["source_path"], lease["token"]))

    def test_late_media_cannot_revive_expired_reservation(self):
        with patch("studio.time.monotonic", return_value=100):
            lease = self.reserve()
        with patch("studio.time.monotonic", return_value=161):
            self.leases.update_media(lease["lease_id"], {"online": True, "inboundBytes": 100,
                "source": {"id": "late-publisher"}})
            row = self.leases.rows()[0]
            self.assertEqual(row["state"], "REVOKING")
            self.assertEqual(row["epoch"], 0)
            self.assertIsNone(row["last_media"])
            self.assertEqual(self.leases.expired(), [lease["lease_id"]])
            self.assertFalse(self.leases.allow_publish(lease["source_path"], lease["token"]))

    def test_reconnect_holds_capacity_and_starts_new_epoch(self):
        lease = self.reserve()
        with patch("studio.time.monotonic", return_value=100):
            self.leases.update_media(lease["lease_id"], {"online": True, "inboundBytes": 100,
                                                      "source": {"id": "first"}})
        with patch("studio.time.monotonic", return_value=101):
            self.leases.update_media(lease["lease_id"], None)
            self.assertEqual(self.leases.rows()[0]["state"], "RECONNECTING")
            self.assertEqual(len(self.leases.rows()), 1)
        with patch("studio.time.monotonic", return_value=105):
            self.leases.update_media(lease["lease_id"], {"online": True, "inboundBytes": 50,
                                                      "source": {"id": "second"}})
        self.assertEqual(self.leases.rows()[0]["epoch"], 2)
        self.assertEqual(self.leases.rows()[0]["state"], "ACTIVE")

    def test_late_media_cannot_revive_expired_reconnect(self):
        with patch("studio.time.monotonic", return_value=100):
            lease = self.reserve()
            self.leases.update_media(lease["lease_id"], {"online": True, "inboundBytes": 100,
                "source": {"id": "first"}})
        with patch("studio.time.monotonic", return_value=101):
            self.leases.update_media(lease["lease_id"], None)
        with patch("studio.time.monotonic", return_value=122):
            self.leases.update_media(lease["lease_id"], {"online": True, "inboundBytes": 50,
                "source": {"id": "late-reconnect"}})
            row = self.leases.rows()[0]
            self.assertEqual(row["state"], "REVOKING")
            self.assertEqual(row["epoch"], 1)
            self.assertEqual(row["publisher_id"], "first")
            self.assertEqual(self.leases.expired(), [lease["lease_id"]])
            self.assertFalse(self.leases.allow_publish(lease["source_path"], lease["token"]))

    def test_connected_publisher_without_media_stays_reserved(self):
        lease = self.reserve()
        self.leases.update_media(lease["lease_id"], {"online": True, "inboundBytes": 0,
                                                  "source": {"id": "connected"}})
        row = self.leases.rows()[0]
        self.assertEqual(row["state"], "RESERVED")
        self.assertIsNone(row["last_media"])


if __name__ == "__main__":
    unittest.main()
