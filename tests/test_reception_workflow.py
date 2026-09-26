import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src import reception_rules as rr


def create_data(**overrides):
    data = {'vessel': 'HaiYun', 'berth': 'B12', 'vessel_length_m': 180, 'berth_length_m': 220,
            'draft_m': 10.2, 'berth_depth_m': 11.5, 'eta_hour': 6, 'etd_hour': 18,
            'risk_level': 'medium', 'dangerous_goods': True, 'dangerous_class': '3类易燃液体',
            'oily_water_tonnes': 3, 'sewage_tonnes': 1}
    data.update(overrides)
    return data


CONTROLLER = Actor("controller", "port_controller")
OPERATOR = Actor("receiver", "reception_operator")


class ReceptionWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _berth(self, reference="VOY-1", **overrides):
        record = self.service.create(CONTROLLER, reference, create_data(**overrides))
        record = self.service.act(CONTROLLER, record["id"], record["version"], "confirm", {"pilot_id": "P-01"})
        record = self.service.act(CONTROLLER, record["id"], record["version"], "berth", {"actual_draft_m": 10.3})
        return record

    def test_full_reception_then_depart(self):
        record = self._berth()
        self.service.register_vehicle(OPERATOR, {"plate": "C-OILY", "capacity_litres": 5000,
                                                 "medium": "oily_water", "service_start_hour": 6, "service_end_hour": 18})
        self.service.register_vehicle(OPERATOR, {"plate": "C-SEW", "capacity_litres": 3000,
                                                 "medium": "sewage", "service_start_hour": 6, "service_end_hour": 18})
        result = self.service.run_scheduling(OPERATOR, record["id"])
        self.assertEqual(len(result["waiting"]), 0)
        slots = self.service.list_slots(OPERATOR, record["id"])
        self.assertEqual(len(slots), 2)
        for slot in slots:
            self.service.register_arrival(OPERATOR, slot["id"], {"actual_litres": slot["planned_litres"]})
        board = self.service.reception_board(OPERATOR, record["id"])
        self.assertEqual(board["waiting"], [])
        record = self.service.repository.get(record["id"])
        departed = self.service.act(CONTROLLER, record["id"], record["version"], "depart",
                                    {"cargo_operation_complete": True})
        self.assertEqual(departed["state"], "departed")

    def test_unsettled_blocks_departure(self):
        record = self._berth("VOY-2")
        with self.assertRaises(Conflict) as ctx:
            self.service.act(CONTROLLER, record["id"], record["version"], "depart",
                             {"cargo_operation_complete": True})
        self.assertIn("污染物接收未结清", str(ctx.exception))

    def test_capacity_shortfall_waiting_reason(self):
        record = self._berth("VOY-3")
        self.service.register_vehicle(OPERATOR, {"plate": "C-OILY", "capacity_litres": 1000,
                                                 "medium": "oily_water", "service_start_hour": 6, "service_end_hour": 18})
        self.service.register_vehicle(OPERATOR, {"plate": "C-SEW", "capacity_litres": 3000,
                                                 "medium": "sewage", "service_start_hour": 6, "service_end_hour": 18})
        result = self.service.run_scheduling(OPERATOR, record["id"])
        waiting = {t["medium"]: t for t in result["waiting"]}
        self.assertIn("oily_water", waiting)
        self.assertIn("容量不足", waiting["oily_water"]["pending_reason"])
        self.assertIn("2,000升", waiting["oily_water"]["pending_reason"])

    def test_short_arrival_rolls_to_next_schedule_and_settles(self):
        record = self._berth("VOY-4", oily_water_tonnes=2, sewage_tonnes=0)
        self.service.register_vehicle(OPERATOR, {"plate": "C-OILY", "capacity_litres": 5000,
                                                 "medium": "oily_water", "service_start_hour": 6, "service_end_hour": 18})
        self.service.run_scheduling(OPERATOR, record["id"])
        first_slot = self.service.list_slots(OPERATOR, record["id"])[0]
        outcome = self.service.register_arrival(OPERATOR, first_slot["id"], {"actual_litres": 800})
        self.assertEqual(outcome["task"]["state"], rr.TASK_WAITING)
        self.assertIn("转下一时段", outcome["task"]["pending_reason"])
        self.assertEqual(outcome["slot"]["state"], rr.SLOT_INCOMPLETE)
        # 第二次排班应滚动到下一可服务时段，且不能与原时段冲突（窗口已前滚一小时）
        self.service.run_scheduling(OPERATOR, record["id"])
        slots = self.service.list_slots(OPERATOR, record["id"])
        self.assertEqual(len(slots), 2)
        self.assertNotEqual(slots[0]["start_hour"], slots[1]["start_hour"])
        self.service.register_arrival(OPERATOR, slots[1]["id"], {"actual_litres": 1200})
        tasks = self.service.reception_tasks(OPERATOR, record["id"])
        self.assertEqual(tasks[0]["state"], rr.TASK_SETTLED)
        self.assertEqual(tasks[0]["received_litres"], 2000)

    def test_same_vehicle_window_not_double_booked(self):
        first = self._berth("VOY-5", oily_water_tonnes=1, sewage_tonnes=0)
        second = self.service.create(CONTROLLER, "VOY-6", create_data(
            vessel='ErYun', berth='B13', oily_water_tonnes=1, sewage_tonnes=0))
        second = self.service.act(CONTROLLER, second["id"], second["version"], "confirm", {"pilot_id": "P-02"})
        second = self.service.act(CONTROLLER, second["id"], second["version"], "berth", {"actual_draft_m": 10.3})
        self.service.register_vehicle(OPERATOR, {"plate": "C-OILY", "capacity_litres": 5000,
                                                 "medium": "oily_water", "service_start_hour": 0, "service_end_hour": 24})
        self.service.run_scheduling(OPERATOR)
        slots = self.service.list_slots(OPERATOR)
        oily_slots = slots
        # 两船被派到不同小时窗口，互不重叠
        windows = sorted((s["start_hour"], s["end_hour"]) for s in oily_slots)
        self.assertEqual(len(windows), 2)
        self.assertTrue(windows[0][1] <= windows[1][0])


if __name__ == "__main__":
    unittest.main()
