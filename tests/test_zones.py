import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import ZONE_QUARANTINE_DAYS, RuleEngine
from src.service import DomainService


def _days_ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


class ZoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("insp-1", "inspector")
        self.viewer = Actor("viewer", "viewer")
        self.center = self.service.create(
            self.admin, "facility", {"name": "苗圃中心", "address": "A县"}
        )
        self.linked = self.service.create(
            self.admin, "facility", {"name": "借苗种植点", "address": "B县"}
        )
        self.outside = self.service.create(
            self.admin, "facility", {"name": "区外种植点", "address": "C县"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _create_zone(self, positive_since=None, **overrides):
        data = {
            "zone_no": "ZQ-2026-001",
            "center_facility_id": self.center["id"],
            "facility_ids": [self.linked["id"]],
            "reason": "中心苗圃检出病虫害，借苗点共用同一批苗",
            "positive_since": positive_since or _days_ago(0),
        }
        data.update(overrides)
        return self.service.create(self.inspector, "zone", data)

    def _create_consignment(self, origin, destination):
        return self.service.create(
            self.admin,
            "consignment",
            {
                "code": "C-100",
                "origin": "苗圃中心",
                "destination": "区外种植点",
                "origin_facility_id": origin,
                "destination_facility_id": destination,
            },
        )

    def test_zone_registration_normalizes_facilities(self):
        zone = self._create_zone()
        self.assertEqual(zone["status"], "active")
        self.assertEqual(
            zone["data"]["facility_ids"], [self.center["id"], self.linked["id"]]
        )
        self.assertEqual(zone["data"]["registered_by"], "insp-1")
        self.assertEqual(
            zone["data"]["facility_reasons"][self.linked["id"]],
            "中心苗圃检出病虫害，借苗点共用同一批苗",
        )
        release = zone["data"]["release"]
        self.assertFalse(release["releaseable"])
        self.assertEqual(release["required_days"], ZONE_QUARANTINE_DAYS)

    def test_duplicate_zone_number_rejected(self):
        self._create_zone()
        with self.assertRaises(ValidationError):
            self._create_zone()

    def test_unknown_facility_rejected(self):
        with self.assertRaises(ValidationError):
            self._create_zone(center_facility_id="no-such-facility")
        with self.assertRaises(ValidationError):
            self._create_zone(facility_ids=["no-such-facility"])

    def test_zone_registration_requires_inspector_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.viewer,
                "zone",
                {
                    "zone_no": "ZQ-2026-002",
                    "center_facility_id": self.center["id"],
                    "facility_ids": [self.linked["id"]],
                    "reason": "viewer 无权登记",
                },
            )

    def test_active_zone_blocks_outbound_shipment(self):
        self._create_zone()
        consignment = self._create_consignment(self.center["id"], self.outside["id"])
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(self.admin, consignment["id"], "ship", {})
        self.assertIn("epidemic zone", str(ctx.exception))
        zone = self.service.list("zone")[0]
        self.assertEqual(
            zone["data"]["blocked_consignment_ids"], [consignment["id"]]
        )

    def test_shipment_inside_zone_allowed(self):
        self._create_zone()
        consignment = self._create_consignment(self.center["id"], self.linked["id"])
        shipped = self.service.transition(self.admin, consignment["id"], "ship", {})
        self.assertEqual(shipped["status"], "shipped")

    def test_shipment_outside_zone_allowed(self):
        self._create_zone()
        other = self.service.create(
            self.admin, "facility", {"name": "无关苗圃", "address": "D县"}
        )
        consignment = self._create_consignment(other["id"], self.outside["id"])
        shipped = self.service.transition(self.admin, consignment["id"], "ship", {})
        self.assertEqual(shipped["status"], "shipped")

    def test_lift_requires_full_waiting_period(self):
        zone = self._create_zone(positive_since=_days_ago(ZONE_QUARANTINE_DAYS - 1))
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(self.inspector, zone["id"], "lift", {})
        self.assertIn("not met", str(ctx.exception))
        old_zone = self._create_zone(
            zone_no="ZQ-2026-003", positive_since=_days_ago(ZONE_QUARANTINE_DAYS)
        )
        lifted = self.service.transition(self.inspector, old_zone["id"], "lift", {})
        self.assertEqual(lifted["status"], "lifted")
        self.assertEqual(lifted["data"]["lifted_by"], "insp-1")

    def test_new_positive_resets_waiting_period(self):
        zone = self._create_zone(positive_since=_days_ago(ZONE_QUARANTINE_DAYS + 5))
        zone = self.service.transition(
            self.inspector,
            zone["id"],
            "mark_positive",
            {"facility_id": self.linked["id"], "date": _days_ago(3)},
        )
        self.assertEqual(zone["data"]["last_positive_date"], _days_ago(3))
        self.assertFalse(zone["data"]["release"]["releaseable"])
        with self.assertRaises(ValidationError):
            self.service.transition(self.inspector, zone["id"], "lift", {})

    def test_new_positive_facility_is_added_to_zone(self):
        zone = self._create_zone(positive_since=_days_ago(1))
        zone = self.service.transition(
            self.inspector,
            zone["id"],
            "mark_positive",
            {"facility_id": self.outside["id"], "date": _days_ago(0), "reason": "复检阳性"},
        )
        self.assertIn(self.outside["id"], zone["data"]["facility_ids"])
        self.assertIn(self.outside["id"], zone["data"]["new_positive_facility_ids"])
        self.assertEqual(
            zone["data"]["facility_reasons"][self.outside["id"]], "复检阳性"
        )
        self.assertFalse(zone["data"]["release"]["no_new_positive_ok"])

    def test_covered_facility_first_positive_counts_as_new(self):
        zone = self._create_zone(positive_since=_days_ago(ZONE_QUARANTINE_DAYS + 2))
        zone = self.service.transition(
            self.inspector,
            zone["id"],
            "mark_positive",
            {"facility_id": self.linked["id"], "date": _days_ago(2)},
        )
        self.assertIn(self.linked["id"], zone["data"]["new_positive_facility_ids"])
        self.assertFalse(zone["data"]["release"]["no_new_positive_ok"])
        with self.assertRaises(ValidationError):
            self.service.transition(self.inspector, zone["id"], "lift", {})

    def test_lifted_zone_allows_reshipment(self):
        zone = self._create_zone(positive_since=_days_ago(ZONE_QUARANTINE_DAYS))
        consignment = self._create_consignment(self.center["id"], self.outside["id"])
        self.service.transition(self.inspector, zone["id"], "lift", {})
        shipped = self.service.transition(self.admin, consignment["id"], "ship", {})
        self.assertEqual(shipped["status"], "shipped")

    def test_lifted_zone_rejects_further_actions(self):
        zone = self._create_zone(positive_since=_days_ago(ZONE_QUARANTINE_DAYS))
        self.service.transition(self.inspector, zone["id"], "lift", {})
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.inspector,
                zone["id"],
                "mark_positive",
                {"facility_id": self.linked["id"]},
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.inspector, zone["id"], "lift", {})

    def test_ship_requires_facility_references(self):
        self._create_zone()
        consignment = self.service.create(
            self.admin,
            "consignment",
            {"code": "C-200", "origin": "X", "destination": "Y"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(self.admin, consignment["id"], "ship", {})


if __name__ == "__main__":
    unittest.main()
