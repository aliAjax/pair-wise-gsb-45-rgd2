import unittest

from src import reception_rules as rr


def task(task_id=1, medium="oily_water", remaining=3000):
    return {"id": task_id, "record_id": 1, "medium": medium, "remaining_litres": remaining,
            "state": rr.TASK_WAITING, "pending_reason": ""}


def vehicle(vid, plate, medium, capacity, start=0, end=24, slots=None):
    return {"id": vid, "plate": plate, "medium": medium, "capacity_litres": capacity,
            "service_start_hour": start, "service_end_hour": end, "active": 1,
            "slots": slots or []}


def slot(task_id, start, end, planned, state=rr.SLOT_ASSIGNED):
    return {"task_id": task_id, "start_hour": start, "end_hour": end,
            "planned_litres": planned, "state": state}


class TonnageTest(unittest.TestCase):
    def test_tonnes_to_litres(self):
        self.assertEqual(rr.litres(1.5), 1500)
        self.assertEqual(rr.parse_demands({"oily_water_tonnes": 2, "sewage_tonnes": 0.3}),
                         {"oily_water": 2000, "sewage": 300})

    def test_vehicle_validation(self):
        payload = {"plate": "沪A-001", "capacity_litres": 5000, "medium": "oily_water",
                   "service_start_hour": 6, "service_end_hour": 18}
        prepared = rr.validate_vehicle(payload)
        self.assertEqual(prepared["capacity_litres"], 5000)


class SchedulingRulesTest(unittest.TestCase):
    def test_compatible_capacity_window_assigns(self):
        t = task()
        vehicles = [vehicle(1, "V1", "oily_water", 5000, 6, 18)]
        plan = rr.plan_task(t, vehicles, [(6, 18)])
        self.assertEqual(plan["vehicle_id"], 1)
        self.assertEqual(plan["planned_litres"], 3000)

    def test_incompatible_medium_stays_waiting(self):
        t = task(medium="oily_water")
        vehicles = [vehicle(1, "V1", "sewage", 5000)]
        plan = rr.plan_task(t, vehicles, [(6, 18)])
        self.assertIsNone(plan)
        self.assertIn("介质不相容", t["pending_reason"])
        self.assertIn("3,000升", t["pending_reason"])

    def test_capacity_shortfall_stated_in_litres(self):
        t = task(remaining=5000)
        vehicles = [vehicle(1, "V1", "oily_water", 3000)]
        plan = rr.plan_task(t, vehicles, [(6, 18)])
        self.assertIsNone(plan)
        self.assertIn("容量不足", t["pending_reason"])
        self.assertIn("还差2,000升", t["pending_reason"])

    def test_same_vehicle_same_window_rejects_two_vessels(self):
        t = task(task_id=2, remaining=3000)
        vehicles = [vehicle(1, "V1", "oily_water", 9000, slots=[slot(99, 8, 10, 1000)])]
        plan = rr.plan_task(t, vehicles, [(8, 10)])
        self.assertIsNone(plan)
        self.assertIn("车辆时段冲突", t["pending_reason"])

    def test_other_window_same_vehicle_is_fine(self):
        t = task(task_id=2, remaining=3000)
        vehicles = [vehicle(1, "V1", "oily_water", 9000, slots=[slot(99, 6, 8, 1000)])]
        plan = rr.plan_task(t, vehicles, [(8, 18)])
        self.assertEqual(plan["vehicle_id"], 1)

    def test_out_of_service_window_falls_back_to_next_hour(self):
        t = task(remaining=3000)
        vehicles = [vehicle(1, "V1", "oily_water", 5000, start=10, end=18)]
        plan = rr.plan_task(t, vehicles, [(h, h + 1) for h in range(6, 18)])
        self.assertEqual(plan["start_hour"], 10)


class SettlementRulesTest(unittest.TestCase):
    def test_short_arrival_rolls_over_with_reason(self):
        t = task(remaining=3000)
        state, reason = rr.settle_arrival(t, 1200)
        self.assertEqual(state, rr.TASK_WAITING)
        self.assertEqual(t["remaining_litres"], 1800)
        self.assertIn("转下一时段", reason)
        self.assertIn("还差1,800升", reason)

    def test_full_arrival_settles(self):
        t = task(remaining=300)
        state, reason = rr.settle_arrival(t, 300)
        self.assertEqual(state, rr.TASK_SETTLED)
        self.assertEqual(t["remaining_litres"], 0)
        self.assertEqual(reason, "")

    def test_actual_cannot_exceed_plan(self):
        with self.assertRaises(Exception):
            rr.validate_arrival({"actual_litres": 4000}, {"planned_litres": 3000}, task(remaining=5000))

    def test_blocking_reasons(self):
        reasons = rr.blocking_reasons([
            {"medium": "oily_water", "state": "waiting", "remaining_litres": 1800},
            {"medium": "sewage", "state": "settled", "remaining_litres": 0},
        ])
        self.assertEqual(len(reasons), 1)
        self.assertIn("污油水", reasons[0])


if __name__ == "__main__":
    unittest.main()
