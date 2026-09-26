"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, number, optional_text, text
from .repository import Repository
from .rules import TASK_PENDING, TASK_SCHEDULED, DomainRules, ReceptionRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, reception_rules: ReceptionRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.reception_rules = reception_rules or ReceptionRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not (self.rules.known_role(actor.role) or self.reception_rules.known_role(actor.role)):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_reception_operator(self, actor: Actor) -> None:
        if not self.reception_rules.role_can_operate(actor.role):
            raise PermissionDenied("角色无权操作污染物接收")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        tasks = self.reception_rules.build_tasks(prepared)
        for task in tasks:
            self.repository.create_reception_task(record["id"], task["medium"], task["declared_liters"])
        if tasks:
            self.audit.note(record["id"], actor.user_id, "reception_declared", {"tasks": tasks})
        return record

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
        if action == "depart":
            self.reception_rules.ensure_depart_clear(self.repository.list_reception_tasks(record_id=record_id))
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def register_vehicle(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_reception_operator(actor)
        data = self.reception_rules.validate_vehicle(payload or {})
        return self.repository.create_vehicle(data, actor.user_id)

    def list_vehicles(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_vehicles()

    def list_reception(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_reception_tasks(record_id=record_id)

    def list_pending_reception(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        tasks = self.repository.list_reception_tasks(state=TASK_PENDING)
        for task in tasks:
            record = self.repository.get(int(task["record_id"]))
            task["reference"] = record["reference"]
            task["vessel"] = record["payload"].get("vessel", "")
        return tasks

    def schedule_reception(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_reception_operator(actor)
        record = self.repository.get(record_id)
        vehicles = self.repository.list_vehicles()
        taken = self.repository.scheduled_slots()
        results = []
        for task in self.repository.list_reception_tasks(record_id=record_id):
            if task["state"] != TASK_PENDING:
                continue
            outcome = self.reception_rules.plan_schedule(task, record["payload"], vehicles, taken)
            updated = self.repository.update_reception_task(task["id"], outcome)
            if outcome["state"] == TASK_SCHEDULED:
                taken.add((outcome["vehicle_no"], int(outcome["slot_hour"])))
            self.audit.note(
                record_id,
                actor.user_id,
                "reception_%s" % outcome["state"],
                {
                    "task_id": task["id"],
                    "medium": task["medium"],
                    "vehicle_no": outcome.get("vehicle_no", ""),
                    "slot_hour": outcome.get("slot_hour"),
                    "pending_reason": outcome.get("pending_reason", ""),
                    "shortfall_liters": outcome.get("shortfall_liters", 0),
                },
            )
            results.append(updated)
        return results

    def receive_reception(self, actor: Actor, task_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_reception_operator(actor)
        payload = payload or {}
        actual = number(payload, "actual_liters", 0)
        if actual <= 0:
            raise ValidationError("actual_liters必须大于0")
        reason = optional_text(payload, "reason")
        task = self.repository.get_reception_task(task_id)
        if task["state"] != TASK_SCHEDULED:
            raise Conflict("任务未处于已排班状态，不能登记实收")
        record = self.repository.get(int(task["record_id"]))
        vehicle = self.repository.get_vehicle(task["vehicle_no"])
        outcome = self.reception_rules.plan_receive(task, record["payload"], vehicle, actual, reason, self.repository.scheduled_slots())
        updated = self.repository.update_reception_task(task_id, outcome)
        self.audit.note(
            int(task["record_id"]),
            actor.user_id,
            "reception_received",
            {"task_id": task_id, "medium": task["medium"], "actual_liters": actual, "state": updated["state"], "summary": outcome.get("summary", "")},
        )
        return updated

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
