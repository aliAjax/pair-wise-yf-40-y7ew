import json
import tempfile
import threading
import unittest
from pathlib import Path

import urllib.request

from src.domain import Actor, ConflictError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine, compute_lockdown, trace_upstream
from src.service import DomainService


def _dt(day):
    return "2026-09-%02dT00:00:00" % day


class LockdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self._seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        self.f1 = self.service.create(self.admin, "facility", {"name": "Greenhouse-1", "address": "County"})
        self.f2 = self.service.create(self.admin, "facility", {"name": "Nursery-2", "address": "County"})
        self.c1 = self.service.create(self.admin, "consignment", {"code": "C-1", "origin": "X", "destination": "Y"})
        self.c2 = self.service.create(self.admin, "consignment", {"code": "C-2", "origin": "X", "destination": "Y"})
        self.service.create(self.admin, "waybill", {
            "code": "WB-1", "consignment_id": self.c1["id"],
            "from_facility_id": self.f2["id"], "to_facility_id": self.f1["id"],
            "shipped_at": _dt(1), "arrived_at": _dt(2),
        })
        self.service.create(self.admin, "stay", {
            "consignment_id": self.c1["id"], "facility_id": self.f1["id"],
            "entered_at": _dt(1), "left_at": _dt(10),
        })
        self.service.create(self.admin, "stay", {
            "consignment_id": self.c2["id"], "facility_id": self.f1["id"],
            "entered_at": _dt(5), "left_at": _dt(15),
        })

    def _quarantine_c1(self):
        self.service.transition(self.admin, self.c1["id"], "inspect",
                                {"inspector": "I-1", "inspection_result": "suspected"})
        self.service.transition(self.admin, self.c1["id"], "quarantine",
                                {"pest_found": True, "sample_id": "S-1"})

    def _scope(self):
        return compute_lockdown(
            {self.c1["id"]}, self.service.list("waybill"), self.service.list("stay"),
            traced_links={self.f1["id"]: [], self.f2["id"]: []},
        )

    def test_positive_batch_freezes_source_and_contacts(self):
        self._quarantine_c1()
        lockdowns = self.service.list("lockdown")
        by_facility = {l["data"]["facility_id"]: l for l in lockdowns}
        self.assertEqual(by_facility[self.f1["id"]]["status"], "frozen")
        self.assertIn(self.c1["id"], by_facility[self.f1["id"]]["data"]["positive_batch_ids"])
        self.assertIn(self.c2["id"], by_facility[self.f1["id"]]["data"]["contact_batch_ids"])
        self.assertEqual(by_facility[self.f2["id"]]["status"], "frozen")
        self.assertEqual(self.service.get(self.c2["id"])["status"], "frozen")

    def test_upstream_trace_via_waybill(self):
        sources = trace_upstream(self.service.list("waybill"), self.c1["id"])
        self.assertEqual(sources, [self.f2["id"]])

    def test_recompute_releases_facility_without_positive_batch(self):
        self._quarantine_c1()
        self.service.transition(self.admin, self.c1["id"], "destroy",
                                {"method": "incineration", "witnessed_by": "W-1"})
        lockdowns = self.service.list("lockdown")
        by_facility = {l["data"]["facility_id"]: l for l in lockdowns}
        self.assertEqual(by_facility[self.f1["id"]]["status"], "released")
        self.assertEqual(by_facility[self.f2["id"]]["status"], "released")
        self.assertEqual(self.service.get(self.c2["id"])["status"], "declared")

    def test_positive_conclusion_keeps_facility_frozen(self):
        self.service.transition(self.admin, self.f1["id"], "conclude",
                                {"pest_found": True, "sample_id": "S-1"})
        lockdowns = self.service.list("lockdown")
        by_facility = {l["data"]["facility_id"]: l for l in lockdowns}
        self.assertEqual(by_facility[self.f1["id"]]["status"], "frozen")

    def test_concurrent_conclude_first_wins(self):
        f1 = self.service.get(self.f1["id"])
        self.service.transition(self.admin, self.f1["id"], "conclude",
                                {"pest_found": True, "sample_id": "S-1"},
                                expected_version=f1["version"])
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, self.f1["id"], "conclude",
                                    {"pest_found": False}, expected_version=f1["version"])

    def test_offline_merge_by_site_time(self):
        records = [
            {"type": "stay", "payload": {"consignment_id": self.c1["id"], "facility_id": self.f1["id"],
                                          "entered_at": _dt(2), "left_at": _dt(3)},
             "site_recorded_at": _dt(2)},
            {"type": "stay", "payload": {"consignment_id": "missing", "facility_id": self.f1["id"],
                                          "entered_at": _dt(2)},
             "site_recorded_at": _dt(2)},
            {"type": "waybill", "payload": {"consignment_id": self.c1["id"],
                                             "from_facility_id": self.f2["id"], "to_facility_id": self.f1["id"],
                                             "shipped_at": _dt(3), "arrived_at": _dt(4)},
             "site_recorded_at": _dt(3)},
        ]
        result = self.service.sync_offline(self.admin, records, "point-A", _dt(2))
        self.assertEqual(len(result["merged"]), 2)
        self.assertEqual(len(result["pending_verification"]), 1)
        self.assertEqual(result["pending_verification"][0]["data"]["reason"], "consignment_not_found")

    def test_offline_conflicting_record_pending(self):
        records = [
            {"type": "stay", "payload": {"consignment_id": self.c1["id"], "facility_id": self.f1["id"],
                                          "entered_at": _dt(1), "left_at": _dt(9)},
             "site_recorded_at": _dt(1)},
        ]
        result = self.service.sync_offline(self.admin, records, "point-A", _dt(1))
        self.assertEqual(len(result["merged"]), 0)
        self.assertEqual(len(result["pending_verification"]), 1)
        self.assertEqual(result["pending_verification"][0]["data"]["reason"], "conflicting_record")

    def test_recompute_on_stay_time_change(self):
        c3 = self.service.create(self.admin, "consignment", {"code": "C-3", "origin": "X", "destination": "Y"})
        self.service.create(self.admin, "stay", {
            "consignment_id": c3["id"], "facility_id": self.f1["id"],
            "entered_at": _dt(20), "left_at": _dt(25),
        })
        self._quarantine_c1()
        self.assertNotIn(c3["id"], self._scope()["contact_by_facility"].get(self.f1["id"], []))
        c1_stay = [s for s in self.service.list("stay")
                   if s["data"]["consignment_id"] == self.c1["id"]][0]
        self.service.transition(self.admin, c1_stay["id"], "correct",
                                {"entered_at": _dt(18), "left_at": _dt(22)})
        self.assertIn(c3["id"], self._scope()["contact_by_facility"].get(self.f1["id"], []))


class HttpOfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.f1 = self.service.create(self.admin, "facility", {"name": "G1", "address": "C"})
        self.c1 = self.service.create(self.admin, "consignment", {"code": "C-1", "origin": "X", "destination": "Y"})
        self.service.create(self.admin, "stay", {
            "consignment_id": self.c1["id"], "facility_id": self.f1["id"],
            "entered_at": _dt(1), "left_at": _dt(10),
        })
        static = Path(__file__).resolve().parent.parent / "static"
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), str(static))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _post(self, path, body):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-User-Id": "admin", "X-Role": "admin"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_offline_sync_http(self):
        result = self._post("/api/offline/sync", {
            "records": [{"type": "stay",
                         "payload": {"consignment_id": self.c1["id"], "facility_id": self.f1["id"], "entered_at": _dt(2)},
                         "site_recorded_at": _dt(2)}],
            "point_id": "point-A",
        })
        self.assertEqual(len(result["merged"]), 1)
        self.assertEqual(len(result["pending_verification"]), 0)
        stays = self._get("/api/stays")["items"]
        self.assertEqual(len(stays), 2)
        self.assertTrue(any(s["data"].get("source") == "offline" for s in stays))

    def test_lockdown_list_http(self):
        self.service.transition(self.admin, self.c1["id"], "inspect",
                                {"inspector": "I-1", "inspection_result": "suspected"})
        self.service.transition(self.admin, self.c1["id"], "quarantine",
                                {"pest_found": True, "sample_id": "S-1"})
        items = self._get("/api/lockdowns")["items"]
        self.assertTrue(items)
        self.assertEqual(items[0]["status"], "frozen")


if __name__ == "__main__":
    unittest.main()
