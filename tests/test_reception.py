import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CONTROLLER = Actor("planner", "port_controller")
OPERATOR = Actor("receiver", "reception_operator")

BERTH_DATA = {
    "vessel": "HaiYun", "berth": "B12", "vessel_length_m": 180, "berth_length_m": 220,
    "draft_m": 10.2, "berth_depth_m": 11.5, "eta_hour": 6, "etd_hour": 18,
    "risk_level": "medium", "dangerous_goods": False, "dangerous_class": "",
    "oily_water_tons": 5, "sewage_tons": 2,
}
VEHICLE = {
    "vehicle_no": "TRUCK-1", "capacity_liters": 8000,
    "media": ["oily_water", "sewage"], "dangerous_classes": [], "slots": [8, 10, 12],
}


class ReceptionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create_record(self, reference="VOY-1", **overrides):
        data = dict(BERTH_DATA)
        data.update(overrides)
        return self.service.create(CONTROLLER, reference, data)

    def _vehicle(self, **overrides):
        data = dict(VEHICLE)
        data.update(overrides)
        return self.service.register_vehicle(OPERATOR, data)

    def _tasks(self, record_id):
        return self.service.list_reception(CONTROLLER, record_id)

    def test_declaration_creates_pending_tasks(self):
        record = self._create_record()
        tasks = self._tasks(record["id"])
        self.assertEqual(len(tasks), 2)
        by_medium = {task["medium"]: task for task in tasks}
        self.assertEqual(by_medium["oily_water"]["declared_liters"], 5000)
        self.assertEqual(by_medium["sewage"]["declared_liters"], 2000)
        for task in tasks:
            self.assertEqual(task["state"], "pending")
            self.assertEqual(task["pending_reason"], "待排班")

    def test_schedule_assigns_vehicle_slots_without_conflict(self):
        record = self._create_record()
        self._vehicle()
        scheduled = {task["medium"]: task for task in self.service.schedule_reception(OPERATOR, record["id"])}
        self.assertEqual(scheduled["oily_water"]["state"], "scheduled")
        self.assertEqual(scheduled["oily_water"]["vehicle_no"], "TRUCK-1")
        self.assertEqual(scheduled["oily_water"]["slot_hour"], 8)
        self.assertEqual(scheduled["sewage"]["state"], "scheduled")
        self.assertEqual(scheduled["sewage"]["slot_hour"], 10)

    def test_same_vehicle_same_slot_cannot_serve_two_ships(self):
        first = self._create_record("VOY-1", sewage_tons=0)
        second = self._create_record("VOY-2", berth="B13", sewage_tons=0)
        self._vehicle(capacity_liters=90000, slots=[8])
        self.service.schedule_reception(OPERATOR, first["id"])
        outcome = self.service.schedule_reception(OPERATOR, second["id"])
        self.assertEqual(outcome[0]["state"], "pending")
        self.assertIn("时段", outcome[0]["pending_reason"])
        self.assertEqual(outcome[0]["shortfall_liters"], 5000)

    def test_incompatible_media_stays_pending_with_shortfall(self):
        record = self._create_record()
        self._vehicle(media=["sewage"])
        outcome = {task["medium"]: task for task in self.service.schedule_reception(OPERATOR, record["id"])}
        oily = outcome["oily_water"]
        self.assertEqual(oily["state"], "pending")
        self.assertIn("介质不相容", oily["pending_reason"])
        self.assertIn("5000", oily["pending_reason"])
        self.assertEqual(oily["shortfall_liters"], 5000)
        self.assertEqual(outcome["sewage"]["state"], "scheduled")

    def test_capacity_shortage_reports_missing_liters(self):
        record = self._create_record(sewage_tons=0)
        self._vehicle(capacity_liters=3000)
        outcome = self.service.schedule_reception(OPERATOR, record["id"])
        self.assertEqual(outcome[0]["state"], "pending")
        self.assertIn("容量不足", outcome[0]["pending_reason"])
        self.assertIn("2000", outcome[0]["pending_reason"])
        self.assertEqual(outcome[0]["shortfall_liters"], 2000)

    def test_dangerous_class_requires_matching_vehicle(self):
        record = self._create_record(sewage_tons=0, dangerous_goods=True, dangerous_class="3")
        self._vehicle()
        outcome = self.service.schedule_reception(OPERATOR, record["id"])
        self.assertEqual(outcome[0]["state"], "pending")
        self.assertIn("危险品类别", outcome[0]["pending_reason"])
        self._vehicle(vehicle_no="TRUCK-2", dangerous_classes=["3"])
        outcome = self.service.schedule_reception(OPERATOR, record["id"])
        self.assertEqual(outcome[0]["state"], "scheduled")
        self.assertEqual(outcome[0]["vehicle_no"], "TRUCK-2")

    def test_partial_receive_carries_over_with_reason(self):
        record = self._create_record()
        self._vehicle()
        scheduled = {task["medium"]: task for task in self.service.schedule_reception(OPERATOR, record["id"])}
        oily = scheduled["oily_water"]
        updated = self.service.receive_reception(OPERATOR, oily["id"], {"actual_liters": 2000, "reason": "船方泵故障"})
        self.assertEqual(updated["state"], "scheduled")
        self.assertEqual(updated["received_liters"], 2000)
        self.assertEqual(updated["slot_hour"], 12)
        self.assertEqual(len(updated["carry_overs"]), 1)
        entry = updated["carry_overs"][0]
        self.assertEqual(entry["from_slot"], 8)
        self.assertEqual(entry["to_slot"], 12)
        self.assertEqual(entry["reason"], "船方泵故障")
        self.assertEqual(entry["remaining_liters"], 3000)
        settled = self.service.receive_reception(OPERATOR, oily["id"], {"actual_liters": 3000})
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(len(settled["carry_overs"]), 1)

    def test_unfinished_without_next_slot_returns_to_pending(self):
        record = self._create_record(sewage_tons=0)
        self._vehicle(slots=[8])
        task = self.service.schedule_reception(OPERATOR, record["id"])[0]
        updated = self.service.receive_reception(OPERATOR, task["id"], {"actual_liters": 1000, "reason": "车辆故障离场"})
        self.assertEqual(updated["state"], "pending")
        self.assertEqual(updated["shortfall_liters"], 4000)
        self.assertIn("时段内未排完", updated["pending_reason"])
        self.assertEqual(updated["carry_overs"][0]["reason"], "车辆故障离场")
        self.assertIsNone(updated["carry_overs"][0]["to_slot"])

    def test_departure_blocked_until_reception_settled(self):
        record = self._create_record()
        record = self.service.act(CONTROLLER, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        record = self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        with self.assertRaises(Conflict):
            self.service.act(CONTROLLER, record["id"], record["version"], "depart", {"cargo_operation_complete": True})
        self._vehicle()
        scheduled = {task["medium"]: task for task in self.service.schedule_reception(OPERATOR, record["id"])}
        self.service.receive_reception(OPERATOR, scheduled["oily_water"]["id"], {"actual_liters": 5000})
        self.service.receive_reception(OPERATOR, scheduled["sewage"]["id"], {"actual_liters": 2000})
        record = self.service.act(CONTROLLER, record["id"], record["version"], "depart", {"cargo_operation_complete": True})
        self.assertEqual(record["state"], "departed")
        actions = [event["action"] for event in self.service.timeline(CONTROLLER, record["id"])]
        self.assertIn("reception_received", actions)

    def test_vehicle_registration_validation_and_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_vehicle(CONTROLLER, VEHICLE)
        with self.assertRaises(ValidationError):
            self._vehicle(media=[])
        with self.assertRaises(ValidationError):
            self._vehicle(slots=[25])
        with self.assertRaises(ValidationError):
            self._vehicle(capacity_liters=0)
        self._vehicle()
        with self.assertRaises(Conflict):
            self._vehicle()

    def test_receive_requires_scheduled_state_and_operator_role(self):
        record = self._create_record(sewage_tons=0)
        self._vehicle()
        task = self._tasks(record["id"])[0]
        with self.assertRaises(Conflict):
            self.service.receive_reception(OPERATOR, task["id"], {"actual_liters": 100})
        self.service.schedule_reception(OPERATOR, record["id"])
        with self.assertRaises(PermissionDenied):
            self.service.receive_reception(CONTROLLER, task["id"], {"actual_liters": 100})
        with self.assertRaises(ValidationError):
            self.service.receive_reception(OPERATOR, task["id"], {"actual_liters": 0})

    def test_pending_area_lists_reasons(self):
        record = self._create_record()
        pending = self.service.list_pending_reception(CONTROLLER)
        self.assertEqual(len(pending), 2)
        self.assertEqual(pending[0]["reference"], "VOY-1")
        self.assertEqual(pending[0]["vessel"], "HaiYun")
        self.assertTrue(pending[0]["pending_reason"])
