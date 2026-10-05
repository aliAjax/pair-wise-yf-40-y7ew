import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LockdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.admin2 = Actor("admin-2", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def _consignment(self, code, **extra):
        data = {"code": code, "origin": "Port-A", "destination": "Farm-B"}
        data.update(extra)
        return self.service.create(self.admin, "consignment", data)

    def _facility(self, name):
        return self.service.create(self.admin, "facility", {"name": name, "address": "County 1"})

    def _stay(self, consignment_id, facility_id, arrived, departed=None):
        data = {
            "consignment_id": consignment_id,
            "facility_id": facility_id,
            "arrived_at": arrived,
        }
        if departed:
            data["departed_at"] = departed
        return self.service.create(self.admin, "stay", data)

    def _quarantine(self, consignment_id):
        self.service.transition(
            self.admin, consignment_id, "inspect",
            {"inspector": "I-1", "inspection_result": "pest"},
        )
        return self.service.transition(
            self.admin, consignment_id, "quarantine",
            {"pest_found": True, "sample_id": "S-1"},
        )

    def test_lockdown_covers_contacts_and_sources(self):
        shed = self._facility("Shed-1")
        other_shed = self._facility("Shed-2")
        upstream = self._consignment("C-U")
        positive = self._consignment("C-P", parent_id=upstream["id"])
        contact = self._consignment("C-C")
        late = self._consignment("C-L")
        far = self._consignment("C-F")
        self._stay(positive["id"], shed["id"], "2026-01-01", "2026-01-10")
        self._stay(contact["id"], shed["id"], "2026-01-05", "2026-01-15")
        self._stay(late["id"], shed["id"], "2026-02-01", "2026-02-05")
        self._stay(far["id"], other_shed["id"], "2026-01-03", "2026-01-08")
        # a second positive batch shares its waybill with a mate
        mate = self._consignment("C-W2", waybill_no="WB-9")
        positive2 = self._consignment("C-P2", waybill_no="WB-9")
        self._stay(positive2["id"], shed["id"], "2026-01-02", "2026-01-09")

        self._quarantine(positive["id"])

        shed_after = self.service.get(shed["id"])
        self.assertEqual(shed_after["status"], "frozen")
        self.assertEqual(shed_after["data"]["freeze_reason"], "lockdown")
        self.assertTrue(self.service.get(contact["id"])["data"]["frozen"])
        self.assertTrue(self.service.get(upstream["id"])["data"]["frozen"])
        self.assertFalse(self.service.get(late["id"])["data"].get("frozen", False))
        self.assertFalse(self.service.get(far["id"])["data"].get("frozen", False))
        self.assertEqual(self.service.get(other_shed["id"])["status"], "registered")

        # waybill mate of a second positive batch is traced as a source
        self._quarantine(positive2["id"])
        self.assertTrue(self.service.get(mate["id"])["data"]["frozen"])

    def test_recompute_after_window_change(self):
        shed = self._facility("Shed-1")
        positive = self._consignment("C-P")
        contact = self._consignment("C-C")
        self._stay(positive["id"], shed["id"], "2026-01-01", "2026-01-10")
        contact_stay = self._stay(contact["id"], shed["id"], "2026-01-05", "2026-01-15")
        self._quarantine(positive["id"])
        self.assertTrue(self.service.get(contact["id"])["data"]["frozen"])
        self.assertEqual(self.service.get(shed["id"])["status"], "frozen")

        # transport window moved: the contact no longer overlaps
        self.service.transition(
            self.admin, contact_stay["id"], "reschedule",
            {"arrived_at": "2026-02-01", "departed_at": "2026-02-10"},
        )
        self.assertFalse(self.service.get(contact["id"])["data"]["frozen"])
        # the shed stays frozen while the positive batch supports it
        self.assertEqual(self.service.get(shed["id"])["status"], "frozen")

        # positive cleared on recheck: the shed is released
        self.service.transition(
            self.admin, positive["id"], "recheck", {"sample_id": "S-2"}
        )
        self.assertEqual(self.service.get(shed["id"])["status"], "registered")

    def test_lift_denied_while_positive_supports_facility(self):
        shed = self._facility("Shed-1")
        positive = self._consignment("C-P")
        self._stay(positive["id"], shed["id"], "2026-01-01", "2026-01-10")
        self._quarantine(positive["id"])
        self.assertEqual(self.service.get(shed["id"])["status"], "frozen")
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, shed["id"], "lift", {})

    def test_first_conclusion_wins(self):
        shed = self._facility("Shed-1")
        first = self.service.transition(
            self.admin, shed["id"], "conclude", {"conclusion": "negative"}
        )
        self.assertEqual(first["data"]["conclusion"], "negative")
        self.assertEqual(first["data"]["concluded_by"], "admin-1")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin2, shed["id"], "conclude", {"conclusion": "positive"}
            )
        # a concurrent submitter holding the stale version also loses
        shed2 = self._facility("Shed-2")
        version = self.service.get(shed2["id"])["version"]
        self.service.transition(
            self.admin, shed2["id"], "conclude",
            {"conclusion": "negative"}, expected_version=version,
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.admin2, shed2["id"], "conclude",
                {"conclusion": "positive"}, expected_version=version,
            )

    def test_positive_conclusion_freezes_facility(self):
        shed = self._facility("Shed-1")
        self.service.transition(
            self.admin, shed["id"], "conclude", {"conclusion": "positive"}
        )
        self.assertEqual(self.service.get(shed["id"])["status"], "frozen")
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, shed["id"], "lift", {})

    def test_dispatch_blocked_when_frozen(self):
        shed = self._facility("Shed-1")
        batch = self._consignment("C-1")
        self._stay(batch["id"], shed["id"], "2026-01-01", "2026-01-10")
        self.service.transition(self.admin, batch["id"], "freeze", {"reason": "manual"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, batch["id"], "dispatch", {})
        self.service.transition(self.admin, batch["id"], "unfreeze", {})
        # the stay is still open-ended? no, it departed; dispatch now allowed
        self.service.transition(self.admin, batch["id"], "dispatch", {})
        self.assertEqual(self.service.get(batch["id"])["status"], "dispatched")

    def test_dispatch_blocked_by_frozen_facility(self):
        shed = self._facility("Shed-1")
        batch = self._consignment("C-1")
        self._stay(batch["id"], shed["id"], "2026-01-01")
        self.service.lockdown(self.admin, shed["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, batch["id"], "dispatch", {})

    def test_sync_merges_by_field_time_and_parks_mismatches(self):
        shed = self._facility("Shed-1")
        shed2 = self._facility("Shed-2")
        batch = self._consignment("C-1")
        other = self._consignment("C-2")
        self._stay(batch["id"], shed["id"], "2026-01-01", "2026-01-10")

        items = [
            # arrives first, but its on-site time is later
            {"consignment_id": other["id"], "facility_id": shed["id"],
             "arrived_at": "2026-01-12", "departed_at": "2026-01-20",
             "occurred_at": "2026-01-12"},
            # earlier on-site time: must be merged before the item above
            {"consignment_id": other["id"], "facility_id": shed2["id"],
             "arrived_at": "2026-01-02", "departed_at": "2026-01-08",
             "occurred_at": "2026-01-02"},
            # exact duplicate of the existing stay
            {"consignment_id": batch["id"], "facility_id": shed["id"],
             "arrived_at": "2026-01-01", "departed_at": "2026-01-10"},
            # overlaps the existing stay at a different shed: cannot be true
            {"consignment_id": batch["id"], "facility_id": shed2["id"],
             "arrived_at": "2026-01-05", "departed_at": "2026-01-08"},
            # references a consignment that does not exist
            {"consignment_id": "no-such-batch", "facility_id": shed["id"],
             "arrived_at": "2026-03-01"},
        ]
        result = self.service.sync_stays(self.admin, items)

        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(len(result["pending"]), 2)
        reasons = sorted(entry["reason"] for entry in result["pending"])
        self.assertTrue(any("overlaps existing stay" in r for r in reasons))
        self.assertTrue(any("unknown consignment" in r for r in reasons))

        # both stays of `other` applied, ordered by on-site time
        self.assertEqual(len(result["applied"]), 2)
        stays = self.repo.find_entities("stay", "consignment_id", other["id"])
        self.assertEqual(len(stays), 2)
        audit = self.repo.list_audit()
        create_order = [
            a["entity_id"] for a in audit
            if a["action"] == "create" and a["entity_id"] in {s["id"] for s in stays}
        ]
        by_arrival = sorted(stays, key=lambda s: s["data"]["arrived_at"])
        self.assertEqual(create_order, [s["id"] for s in by_arrival])

        # pending items are listed for manual verification
        pending = self.service.list_pending()
        self.assertEqual(len(pending), 2)

    def test_resolve_pending(self):
        shed = self._facility("Shed-1")
        batch = self._consignment("C-1")
        self._stay(batch["id"], shed["id"], "2026-01-01", "2026-01-10")
        result = self.service.sync_stays(self.admin, [
            {"consignment_id": batch["id"], "facility_id": shed["id"],
             "arrived_at": "2026-01-05", "departed_at": "2026-01-08"},
        ])
        self.assertEqual(len(result["pending"]), 1)
        review_id = result["pending"][0]["id"]

        # admin verifies on site and accepts the registration
        resolved = self.service.resolve_pending(self.admin, review_id, "accept")
        self.assertEqual(resolved["review"]["status"], "accepted")
        self.assertIsNotNone(resolved["stay"])
        with self.assertRaises(ConflictError):
            self.service.resolve_pending(self.admin, review_id, "discard")

    def test_sync_requires_field_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.sync_stays(Actor("v", "viewer"), [{"consignment_id": "x"}])


if __name__ == "__main__":
    unittest.main()
