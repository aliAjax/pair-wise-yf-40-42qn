import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine, zone_release_status
from src.service import DomainService

DECLARED = "2026-09-01"
ELIGIBLE_DATE = "2026-09-22"  # 21 days after declaration


class ZoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("inspector-1", "inspector")
        self.quarantine = Actor("quarantine-1", "quarantine")
        self.viewer = Actor("viewer-1", "viewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _facility(self, name):
        return self.service.create(
            self.admin, "facility", {"name": name, "address": name + " county"}
        )

    def _zone(self, declared_at=DECLARED, **overrides):
        center = self._facility("center")
        linked = self._facility("linked")
        payload = {
            "code": "Z-1",
            "center_facility_id": center["id"],
            "facilities": [{"facility_id": linked["id"], "reason": "共用过同一批苗"}],
            "declared_at": declared_at,
        }
        payload.update(overrides)
        zone = self.service.create(self.inspector, "zone", payload)
        return zone, center, linked

    def _consignment(self, origin, destination, code="C-1"):
        return self.service.create(
            self.inspector,
            "consignment",
            {
                "code": code,
                "origin": "origin",
                "destination": "destination",
                "origin_facility_id": origin["id"],
                "destination_facility_id": destination["id"],
            },
        )

    def test_inspector_registers_zone_with_center_facilities_and_reasons(self):
        zone, center, linked = self._zone()
        self.assertEqual(zone["status"], "active")
        self.assertEqual(zone["data"]["center_facility_id"], center["id"])
        self.assertEqual(zone["data"]["facilities"][0]["facility_id"], linked["id"])
        self.assertEqual(zone["data"]["facilities"][0]["reason"], "共用过同一批苗")
        self.assertEqual(zone["data"]["declared_at"], DECLARED)
        self.assertEqual(
            zone["data"]["positive_events"],
            [{"facility_id": center["id"], "at": DECLARED}],
        )

    def test_zone_code_must_be_unique(self):
        self._zone()
        center = self._facility("other center")
        linked = self._facility("other linked")
        with self.assertRaises(ConflictError):
            self.service.create(
                self.inspector,
                "zone",
                {
                    "code": "Z-1",
                    "center_facility_id": center["id"],
                    "facilities": [
                        {"facility_id": linked["id"], "reason": "共用车辆"}
                    ],
                },
            )

    def test_center_facility_must_exist(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.inspector,
                "zone",
                {
                    "code": "Z-2",
                    "center_facility_id": "missing",
                    "facilities": [],
                },
            )

    def test_each_included_facility_needs_a_reason(self):
        center = self._facility("center only")
        linked = self._facility("no reason")
        with self.assertRaises(ValidationError):
            self.service.create(
                self.inspector,
                "zone",
                {
                    "code": "Z-3",
                    "center_facility_id": center["id"],
                    "facilities": [{"facility_id": linked["id"]}],
                },
            )

    def test_viewer_cannot_register_zone(self):
        center = self._facility("center v")
        linked = self._facility("linked v")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.viewer,
                "zone",
                {
                    "code": "Z-4",
                    "center_facility_id": center["id"],
                    "facilities": [
                        {"facility_id": linked["id"], "reason": "借苗"}
                    ],
                },
            )

    def test_dispatch_out_of_active_zone_is_blocked(self):
        zone, center, linked = self._zone()
        outside = self._facility("outside")
        batch = self._consignment(center, outside, "OUT")
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(self.quarantine, batch["id"], "dispatch")
        self.assertIn("Z-1", str(ctx.exception))

    def test_dispatch_within_zone_is_allowed(self):
        zone, center, linked = self._zone()
        batch = self._consignment(center, linked, "IN")
        updated = self.service.transition(self.quarantine, batch["id"], "dispatch")
        self.assertEqual(updated["status"], "dispatched")

    def test_dispatch_into_zone_from_outside_is_allowed(self):
        zone, center, linked = self._zone()
        outside = self._facility("outside")
        batch = self._consignment(outside, center, "INBOUND")
        updated = self.service.transition(self.quarantine, batch["id"], "dispatch")
        self.assertEqual(updated["status"], "dispatched")

    def test_dispatch_unblocked_after_zone_lifted(self):
        zone, center, linked = self._zone()
        outside = self._facility("outside")
        batch = self._consignment(center, outside, "OUT2")
        with self.assertRaises(ValidationError):
            self.service.transition(self.quarantine, batch["id"], "dispatch")
        self.service.transition(
            self.inspector, zone["id"], "lift", {"as_of": ELIGIBLE_DATE}
        )
        updated = self.service.transition(self.quarantine, batch["id"], "dispatch")
        self.assertEqual(updated["status"], "dispatched")

    def test_cannot_lift_before_21_days_have_passed(self):
        zone, _, _ = self._zone(declared_at="2026-09-28")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.inspector, zone["id"], "lift", {"as_of": "2026-10-18"}
            )

    def test_new_positive_facility_resets_the_21_day_clock(self):
        zone, center, linked = self._zone(declared_at="2026-09-01")
        newcomer = self._facility("newcomer")
        # 21 days after declaration but a fresh positive was found on day 10
        zone = self.service.transition(
            self.inspector,
            zone["id"],
            "report_positive",
            {
                "facility_id": newcomer["id"],
                "positive_at": "2026-09-11",
                "reason": "共用过车辆",
            },
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.inspector, zone["id"], "lift", {"as_of": ELIGIBLE_DATE}
            )
        lifted = self.service.transition(
            self.inspector, zone["id"], "lift", {"as_of": "2026-10-02"}
        )
        self.assertEqual(lifted["status"], "lifted")
        self.assertEqual(lifted["data"]["lifted_at"], "2026-10-02")

    def test_report_positive_adds_facility_and_event(self):
        zone, center, linked = self._zone()
        newcomer = self._facility("newly positive")
        updated = self.service.transition(
            self.inspector,
            zone["id"],
            "report_positive",
            {"facility_id": newcomer["id"], "positive_at": "2026-09-05"},
        )
        reasons = {
            item["facility_id"]: item["reason"]
            for item in updated["data"]["facilities"]
        }
        self.assertEqual(reasons[newcomer["id"]], "newly positive facility")
        self.assertEqual(len(updated["data"]["positive_events"]), 2)

    def test_cannot_lift_an_already_lifted_zone(self):
        zone, _, _ = self._zone()
        self.service.transition(
            self.inspector, zone["id"], "lift", {"as_of": ELIGIBLE_DATE}
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.inspector, zone["id"], "lift", {"as_of": ELIGIBLE_DATE}
            )

    def test_viewer_cannot_lift_zone(self):
        zone, _, _ = self._zone()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.viewer, zone["id"], "lift", {"as_of": ELIGIBLE_DATE}
            )

    def test_zone_detail_shows_facilities_and_release_conditions(self):
        zone, center, linked = self._zone()
        detail = self.service.zone_detail(zone["id"], "2026-09-10")
        self.assertEqual(len(detail["facilities"]), 2)
        roles = {item["facility_id"]: item["role"] for item in detail["facilities"]}
        self.assertEqual(roles[center["id"]], "center")
        self.assertEqual(roles[linked["id"]], "included")
        self.assertFalse(detail["release"]["eligible"])
        keys = {item["key"] for item in detail["release"]["conditions"]}
        self.assertEqual(
            keys, {"observation_elapsed", "no_new_positive_facility"}
        )

        detail_ready = self.service.zone_detail(zone["id"], ELIGIBLE_DATE)
        self.assertTrue(detail_ready["release"]["eligible"])

    def test_release_status_helper_flags_new_positive_facility(self):
        data = {
            "declared_at": "2026-09-01",
            "positive_events": [
                {"facility_id": "f1", "at": "2026-09-01"},
                {"facility_id": "f2", "at": "2026-09-20"},
            ],
        }
        blocked = zone_release_status(data, "2026-09-22")
        self.assertFalse(blocked["eligible"])
        self.assertEqual(
            [item["facility_id"] for item in blocked["new_positive_facilities"]],
            ["f2"],
        )
        ready = zone_release_status(data, "2026-10-11")
        self.assertTrue(ready["eligible"])


if __name__ == "__main__":
    unittest.main()
