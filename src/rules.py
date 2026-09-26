"""港口泊位与航道调度领域规则、状态转换与污染物接收排班判定。"""
from typing import Any, Dict, Iterable, List, Set, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, integer_list, number, optional_number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        p["oily_water_tons"] = optional_number(p, "oily_water_tons", 0.0, 0)
        p["sewage_tons"] = optional_number(p, "sewage_tons", 0.0, 0)
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)


WASTE_MEDIA = {"oily_water": "污油水", "sewage": "生活污水"}
LITERS_PER_TON = 1000
TASK_PENDING = "pending"
TASK_SCHEDULED = "scheduled"
TASK_SETTLED = "settled"
RECEPTION_ROLES = {"reception_operator"}


def liters_text(value: float) -> str:
    value = round(float(value), 2)
    if float(value).is_integer():
        return str(int(value))
    return ("%f" % value).rstrip("0").rstrip(".")


class ReceptionRules:
    """污染物接收排班判定：介质适配、危险品类别、罐容与时段冲突。"""

    def known_role(self, role: str) -> bool:
        return role in RECEPTION_ROLES

    def role_can_operate(self, role: str) -> bool:
        return role == "admin" or role in RECEPTION_ROLES

    def validate_vehicle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        vehicle_no = text(p, "vehicle_no")
        capacity = number(p, "capacity_liters", 1)
        media = sorted(set(text_list(p, "media", 1)))
        for medium in media:
            if medium not in WASTE_MEDIA:
                raise ValidationError("media只能是%s" % "/".join(sorted(WASTE_MEDIA)))
        dangerous_classes = sorted(set(text_list(p, "dangerous_classes")))
        slots = sorted(set(integer_list(p, "slots", 1, 0, 23)))
        return {
            "vehicle_no": vehicle_no,
            "capacity_liters": capacity,
            "media": media,
            "dangerous_classes": dangerous_classes,
            "slots": slots,
        }

    def build_tasks(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        tasks = []
        for medium, key in (("oily_water", "oily_water_tons"), ("sewage", "sewage_tons")):
            tons = float(payload.get(key) or 0)
            if tons > 0:
                tasks.append({"medium": medium, "declared_liters": round(tons * LITERS_PER_TON, 2)})
        return tasks

    @staticmethod
    def _remaining(task: Dict[str, Any]) -> float:
        return round(float(task["declared_liters"]) - float(task["received_liters"]), 2)

    @staticmethod
    def _pending(reason: str, remaining: float, shortfall: float = None) -> Dict[str, Any]:
        return {
            "state": TASK_PENDING,
            "vehicle_no": "",
            "slot_hour": None,
            "pending_reason": reason,
            "shortfall_liters": round(remaining if shortfall is None else shortfall, 2),
        }

    def plan_schedule(self, task: Dict[str, Any], ship: Dict[str, Any], vehicles: List[Dict[str, Any]], taken_slots: Set[Tuple[str, int]]) -> Dict[str, Any]:
        remaining = self._remaining(task)
        dangerous_class = str(ship.get("dangerous_class") or "") if ship.get("dangerous_goods") else ""
        eta, etd = int(ship["eta_hour"]), int(ship["etd_hour"])
        medium_ok = [v for v in vehicles if task["medium"] in v["media"]]
        if not medium_ok:
            return self._pending("介质不相容：无适配%s的接收车，还差%s升" % (WASTE_MEDIA[task["medium"]], liters_text(remaining)), remaining)
        compatible = [v for v in medium_ok if not dangerous_class or dangerous_class in v["dangerous_classes"]]
        if not compatible:
            return self._pending("危险品类别%s无适配接收车，还差%s升" % (dangerous_class, liters_text(remaining)), remaining)
        options = []
        for vehicle in compatible:
            for slot in vehicle["slots"]:
                if eta <= slot < etd and (vehicle["vehicle_no"], slot) not in taken_slots:
                    options.append((slot, vehicle["vehicle_no"], vehicle))
        if not options:
            return self._pending("可服务时段已被排满，还差%s升" % liters_text(remaining), remaining)
        best_capacity = max(vehicle["capacity_liters"] for _, _, vehicle in options)
        if best_capacity < remaining:
            shortfall = round(remaining - best_capacity, 2)
            return self._pending("容量不足：可用罐容最大%s升，还差%s升" % (liters_text(best_capacity), liters_text(shortfall)), remaining, shortfall)
        options.sort(key=lambda item: (item[0], item[1]))
        for slot, vehicle_no, vehicle in options:
            if vehicle["capacity_liters"] >= remaining:
                return {"state": TASK_SCHEDULED, "vehicle_no": vehicle_no, "slot_hour": slot, "pending_reason": "", "shortfall_liters": 0.0}
        return self._pending("容量不足：还差%s升" % liters_text(remaining), remaining)

    def plan_receive(self, task: Dict[str, Any], ship: Dict[str, Any], vehicle: Dict[str, Any], actual_liters: float, reason: str, taken_slots: Set[Tuple[str, int]]) -> Dict[str, Any]:
        received = round(float(task["received_liters"]) + actual_liters, 2)
        remaining = round(float(task["declared_liters"]) - received, 2)
        carry_overs = list(task.get("carry_overs") or [])
        if remaining <= 0:
            return {
                "state": TASK_SETTLED,
                "received_liters": received,
                "pending_reason": "",
                "shortfall_liters": 0.0,
                "carry_overs": carry_overs,
                "summary": "实收%s升，%s已结清" % (liters_text(actual_liters), WASTE_MEDIA[task["medium"]]),
            }
        note = (reason or "").strip() or "实收不足"
        current = task.get("slot_hour")
        etd = int(ship["etd_hour"])
        entry = {"from_slot": current, "remaining_liters": remaining, "reason": note}
        candidates = [
            slot
            for slot in vehicle["slots"]
            if current is not None and slot > current and slot < etd and (vehicle["vehicle_no"], slot) not in taken_slots
        ]
        if candidates:
            next_slot = min(candidates)
            entry["to_slot"] = next_slot
            carry_overs.append(entry)
            return {
                "state": TASK_SCHEDULED,
                "received_liters": received,
                "vehicle_no": vehicle["vehicle_no"],
                "slot_hour": next_slot,
                "pending_reason": "",
                "shortfall_liters": 0.0,
                "carry_overs": carry_overs,
                "summary": "实收%s升，剩余%s升转下一时段（%s时）：%s" % (liters_text(actual_liters), liters_text(remaining), next_slot, note),
            }
        entry["to_slot"] = None
        carry_overs.append(entry)
        return {
            "state": TASK_PENDING,
            "received_liters": received,
            "vehicle_no": "",
            "slot_hour": None,
            "pending_reason": "时段内未排完：%s，还差%s升" % (note, liters_text(remaining)),
            "shortfall_liters": remaining,
            "carry_overs": carry_overs,
            "summary": "实收%s升，剩余%s升留待收区：%s" % (liters_text(actual_liters), liters_text(remaining), note),
        }

    def ensure_depart_clear(self, tasks: List[Dict[str, Any]]) -> None:
        outstanding = [task for task in tasks if task["state"] != TASK_SETTLED]
        if not outstanding:
            return
        parts = []
        for task in outstanding:
            label = WASTE_MEDIA.get(task["medium"], task["medium"])
            reason = task.get("pending_reason") or ("已排班待接收" if task["state"] == TASK_SCHEDULED else "")
            part = "%s还差%s升" % (label, liters_text(self._remaining(task)))
            parts.append("%s（%s）" % (part, reason) if reason else part)
        raise Conflict("污染物接收未结清，不能离泊：" + "；".join(parts))
