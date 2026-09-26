"""业务用例编排、权限检查与审计。"""
import threading
from typing import Any, Dict, List, Optional

from . import reception_rules as rr
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import RECEPTION_ROLES, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        # 排班涉及同一事务内的多任务匹配，进程内串行化避免并发互派
        self._schedule_lock = threading.Lock()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        # 离泊前结清：两种污水未排完不允许离泊
        if action == "depart":
            blocking = rr.blocking_reasons(self.repository.list_tasks(record_id=record_id))
            if blocking:
                raise Conflict("污染物接收未结清，不允许离泊：%s" % "；".join(blocking))
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        # 靠泊确认后按申报吨数生成两种污水的接收任务
        if action == "berth":
            self._ensure_reception_tasks(actor, updated)
        return updated

    def _ensure_reception_tasks(self, actor: Actor, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        demands = rr.parse_demands(record["payload"])
        tasks = self.repository.ensure_tasks_for_record(record, demands, actor.user_id)
        if tasks:
            self.audit.note(record["id"], actor.user_id, "reception_opened", {
                "demands_litres": {t["medium"]: t["demand_litres"] for t in tasks},
                "summary": "已按申报生成污染物接收任务",
            })
        return tasks

    # ---- 污染物接收排班 ----

    def _reception_allowed(self, actor: Actor) -> None:
        if actor.role != "admin" and actor.role not in RECEPTION_ROLES:
            raise PermissionDenied("角色无权操作污染物接收")

    def register_vehicle(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._reception_allowed(actor)
        prepared = rr.validate_vehicle(payload or {})
        return self.repository.create_vehicle(prepared, actor.user_id)

    def list_vehicles(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._reception_allowed(actor)
        return self.repository.list_vehicles()

    def set_vehicle_active(self, actor: Actor, vehicle_id: int, active: bool) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._reception_allowed(actor)
        return self.repository.set_vehicle_active(vehicle_id, active)

    def reception_tasks(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._reception_allowed(actor)
        self.repository.get(record_id)
        return self.repository.list_tasks(record_id=record_id)

    def waiting_tasks(self, actor: Actor) -> List[Dict[str, Any]]:
        """待收区：所有未排上或未结清的任务，保留原因。"""
        actor = self._actor(actor)
        self._reception_allowed(actor)
        return self.repository.list_tasks(state="waiting")

    def run_scheduling(self, actor: Actor, record_id: Optional[int] = None) -> Dict[str, Any]:
        """对待收任务执行排班；只排班，不改变已结清任务。"""
        actor = self._actor(actor)
        self._reception_allowed(actor)

        def planner(tasks: List[Dict[str, Any]], vehicles: List[Dict[str, Any]],
                    payloads: Dict[int, Dict[str, Any]]) -> List:
            operations: List[Any] = []
            for task in tasks:
                if task["state"] != rr.TASK_WAITING or int(task["remaining_litres"]) <= 0:
                    continue
                if record_id is not None and int(task["record_id"]) != int(record_id):
                    continue
                payload = payloads.get(int(task["record_id"]))
                if payload is None:
                    continue
                working = dict(task)
                windows = rr.vessel_windows(payload, int(task.get("skip_until_hour", 0)))
                plan = rr.plan_task(working, vehicles, windows)
                if plan is None:
                    operations.append(("task", task["id"], rr.TASK_WAITING, working["pending_reason"]))
                    continue
                operations.append(("slot", task["id"], plan))
                operations.append(("task", task["id"], rr.TASK_SCHEDULED, ""))
                # 本次新占用即时反映到车辆视图，避免同批任务把同一时段派给两船
                for vehicle in vehicles:
                    if vehicle["id"] == plan["vehicle_id"]:
                        vehicle["slots"].append({
                            "task_id": task["id"], "state": rr.SLOT_ASSIGNED,
                            "start_hour": plan["start_hour"], "end_hour": plan["end_hour"],
                            "planned_litres": plan["planned_litres"],
                        })
                        break
            return operations

        with self._schedule_lock:
            tasks = self.repository.run_scheduling(planner, actor.user_id)
        if record_id is not None:
            tasks = [t for t in tasks if int(t["record_id"]) == int(record_id)]
        return {"items": tasks, "waiting": [t for t in tasks if t["state"] == rr.TASK_WAITING]}

    def list_slots(self, actor: Actor, record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._reception_allowed(actor)
        if record_id is None:
            return self.repository.list_slots()
        task_ids = {t["id"] for t in self.repository.list_tasks(record_id=record_id)}
        return [slot for slot in self.repository.list_slots() if slot["task_id"] in task_ids]

    def register_arrival(self, actor: Actor, slot_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """到场登记实收量；未排完转下一时段并保留原因，结清前可再次排班。"""
        actor = self._actor(actor)
        self._reception_allowed(actor)
        slot = self.repository.get_slot(slot_id)
        task = self.repository.get_task(int(slot["task_id"]))
        if slot["state"] != rr.SLOT_ASSIGNED:
            raise Conflict("该排班已登记到场")
        actual = rr.validate_arrival(data or {}, slot, task)
        working = dict(task)
        new_state, reason = rr.settle_arrival(working, actual)
        skip_until = int(slot["end_hour"]) if new_state == rr.TASK_WAITING else None
        new_slot, new_task = self.repository.arrive(
            slot_id=slot_id,
            actual_litres=actual,
            task_state=new_state,
            pending_reason=reason,
            remaining_litres=int(working["remaining_litres"]),
            received_litres=int(task["received_litres"]) + actual,
            skip_until_hour=skip_until,
        )
        self.audit.note(int(task["record_id"]), actor.user_id, "reception_arrival", {
            "slot_id": slot_id, "medium": task["medium"], "actual_litres": actual,
            "task_state": new_state, "pending_reason": reason,
        })
        return {"slot": new_slot, "task": new_task}

    def reception_board(self, actor: Actor, record_id: Optional[int] = None) -> Dict[str, Any]:
        """看板：待收区（含原因与缺口升数）与已排班时段。"""
        actor = self._actor(actor)
        self._reception_allowed(actor)
        records = {r["id"]: r for r in self.repository.list_records(limit=500)}
        tasks = self.repository.list_tasks(record_id=record_id)
        slots = self.repository.list_slots()
        vehicles = {v["id"]: v for v in self.repository.list_vehicles()}
        task_ids = {t["id"] for t in tasks}
        board_slots = []
        for slot in slots:
            if slot["task_id"] not in task_ids:
                continue
            task = next(t for t in tasks if t["id"] == slot["task_id"])
            record = records.get(task["record_id"], {})
            vehicle = vehicles.get(slot["vehicle_id"], {})
            board_slots.append({
                "slot_id": slot["id"],
                "record_id": task["record_id"],
                "vessel": record.get("payload", {}).get("vessel", ""),
                "medium": task["medium"],
                "medium_label": rr.MEDIA_LABELS[task["medium"]],
                "plate": vehicle.get("plate", ""),
                "window": "%02d:00-%02d:00" % (slot["start_hour"], slot["end_hour"] if slot["end_hour"] < 24 else 0),
                "planned_litres": slot["planned_litres"],
                "actual_litres": slot["actual_litres"],
                "state": slot["state"],
            })
        waiting = [{
            "task_id": t["id"],
            "record_id": t["record_id"],
            "vessel": records.get(t["record_id"], {}).get("payload", {}).get("vessel", ""),
            "medium": t["medium"],
            "medium_label": rr.MEDIA_LABELS[t["medium"]],
            "remaining_litres": t["remaining_litres"],
            "pending_reason": t["pending_reason"],
        } for t in tasks if t["state"] == rr.TASK_WAITING]
        return {"waiting": waiting, "slots": board_slots,
                "stats": self.repository.reception_stats()}

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
