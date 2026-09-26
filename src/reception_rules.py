"""污染物接收排班判定：资料解析、车辆适配、排班与实收结算。

纯函数模块，不访问数据库。吨为申报单位，升为结算单位：1吨=1000升。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import ValidationError, choice, integer, number, optional_number, text


# 两种接收污水
MEDIA = ("oily_water", "sewage")
MEDIA_LABELS = {"oily_water": "污油水", "sewage": "生活污水"}

LITRES_PER_TONNE = 1000

# 排班/待收原因
REASON_NO_VEHICLE = "无适配介质车辆"
REASON_INCOMPATIBLE = "介质不相容"
REASON_CAPACITY_SHORT = "容量不足"
REASON_BUSY = "车辆时段冲突"
REASON_WINDOW = "不在可服务时段"
REASON_WINDOWS_EXHAUSTED = "靠泊时段内无可用排班窗口"
REASON_ARRIVED_SHORT = "到场实收不足，转下一时段"

# 原因优先级：车队硬约束优先于可随下一时段消解的冲突
_PRIORITY = {REASON_CAPACITY_SHORT: 1, REASON_BUSY: 2, REASON_WINDOW: 3, REASON_WINDOWS_EXHAUSTED: 4}

SLOT_ASSIGNED = "assigned"      # 已排班，等待到场
SLOT_ARRIVED = "arrived"        # 已到场登记实收
SLOT_INCOMPLETE = "incomplete"  # 实收不足，留痕（任务转下一时段）

TASK_WAITING = "waiting"        # 待收区
TASK_SCHEDULED = "scheduled"    # 已有排班
TASK_SETTLED = "settled"        # 结清


def litres(value_tonnes: float) -> int:
    """吨转升（调度与结算统一使用整数升）。"""
    return int(round(float(value_tonnes) * LITRES_PER_TONNE))


def format_litres(value: float) -> str:
    return "%s升" % format(int(round(float(value))), ",")


def parse_demands(payload: Dict[str, Any]) -> Dict[str, int]:
    """从靠泊计划申报资料解析两种污水需求量（升）。缺失按0处理。"""
    return {media: litres(optional_number(payload, "%s_tonnes" % media, 0.0, 0)) for media in MEDIA}


def validate_vehicle(payload: Dict[str, Any]) -> Dict[str, Any]:
    """接收车登记资料：罐容（升）、适配介质、可服务时段。"""
    p = dict(payload)
    plate = text(p, "plate")
    capacity = number(p, "capacity_litres", 1)
    medium = choice(p, "medium", list(MEDIA))
    start = integer(p, "service_start_hour", 0, 23)
    end = integer(p, "service_end_hour", 1, 24)
    if end <= start:
        raise ValidationError("service_end_hour必须晚于service_start_hour")
    note = p.get("note", "")
    if note is not None and not isinstance(note, str):
        raise ValidationError("note必须是文本")
    return {
        "plate": plate,
        "capacity_litres": int(capacity),
        "medium": medium,
        "service_start_hour": start,
        "service_end_hour": end,
        "note": (note or "").strip(),
    }


def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def _occupied(vehicle: Dict[str, Any], window_start: int, window_end: int, task_id: int) -> bool:
    """车辆在该时段是否已服务其他船（同一辆车同一时段不能接两船）。"""
    for slot in vehicle.get("slots", []):
        if slot.get("task_id") == task_id or slot.get("state") == SLOT_INCOMPLETE:
            continue
        if overlaps(window_start, window_end, int(slot["start_hour"]), int(slot["end_hour"])):
            return True
    return False


def plan_task(
    task: Dict[str, Any],
    vehicles: Iterable[Dict[str, Any]],
    windows: Iterable[Tuple[int, int]],
) -> Optional[Dict[str, Any]]:
    """为单船单介质任务排班。

    逐时段匹配：介质相容 → 车辆可服务时段 → 同时段未服务他船 → 罐容足够。
    成功返回 {vehicle_id, start_hour, end_hour, planned_litres}；
    失败返回None，并把原因（含还差多少升）写入 task["pending_reason"]。
    """
    medium = task["medium"]
    remaining = int(task["remaining_litres"])
    compatible = [v for v in vehicles if v.get("active", 1) and v["medium"] == medium]
    if not compatible:
        if any(v.get("active", 1) for v in vehicles):
            task["pending_reason"] = "%s：%s，还差%s" % (
                REASON_INCOMPATIBLE, MEDIA_LABELS[medium], format_litres(remaining))
        else:
            task["pending_reason"] = "%s，还差%s" % (REASON_NO_VEHICLE, format_litres(remaining))
        return None

    fallback: Optional[Tuple[int, str]] = None
    tried = False
    for window_start, window_end in windows:
        if window_end <= window_start:
            continue
        tried = True
        candidates = [
            v for v in compatible
            if overlaps(window_start, window_end, v["service_start_hour"], v["service_end_hour"])
        ]
        if not candidates:
            reason = "%s，还差%s" % (REASON_WINDOW, format_litres(remaining))
            fallback = _better(fallback, (_PRIORITY[REASON_WINDOW], reason))
            continue
        free_vehicles = [v for v in candidates if not _occupied(v, window_start, window_end, task["id"])]
        if not free_vehicles:
            reason = "%s：%s时段已接他船，还差%s" % (
                REASON_BUSY, _window_label(window_start, window_end), format_litres(remaining))
            fallback = _better(fallback, (_PRIORITY[REASON_BUSY], reason))
            continue
        fitting = [v for v in free_vehicles if int(v["capacity_litres"]) >= remaining]
        if fitting:
            vehicle = min(fitting, key=lambda v: int(v["capacity_litres"]))
            return {
                "vehicle_id": vehicle["id"],
                "start_hour": window_start,
                "end_hour": window_end,
                "planned_litres": remaining,
            }
        # 介质相容且时段空闲，但所有车罐容都不足：写明最接近的车还差多少升
        vehicle = min(free_vehicles, key=lambda v: remaining - int(v["capacity_litres"]))
        shortfall = remaining - int(vehicle["capacity_litres"])
        reason = "%s：车%s罐容%s，%s还差%s" % (
            REASON_CAPACITY_SHORT, vehicle["plate"], format_litres(vehicle["capacity_litres"]),
            MEDIA_LABELS[medium], format_litres(shortfall))
        fallback = _better(fallback, (_PRIORITY[REASON_CAPACITY_SHORT], reason))

    task["pending_reason"] = fallback[1] if fallback else (
        "%s，还差%s" % (REASON_WINDOWS_EXHAUSTED, format_litres(remaining)) if tried
        else "%s，还差%s" % (REASON_NO_VEHICLE, format_litres(remaining)))
    return None


def _better(current: Optional[Tuple[int, str]], candidate: Tuple[int, str]) -> Tuple[int, str]:
    if current is None or candidate[0] < current[0]:
        return candidate
    return current


def _window_label(start_hour: int, end_hour: int) -> str:
    return "%02d:00-%02d:00" % (start_hour, end_hour if end_hour < 24 else 0)


def vessel_windows(record_payload: Dict[str, Any], skip_until_hour: int = 0) -> List[Tuple[int, int]]:
    """靠泊时段内逐小时候选窗口；未排完的任务从skip_until_hour起滚动到下一时段继续匹配。"""
    eta = int(record_payload["eta_hour"])
    etd = int(record_payload["etd_hour"])
    start = max(eta, int(skip_until_hour))
    return [(h, min(h + 1, etd)) for h in range(start, etd)] or [(eta, etd)]


def validate_arrival(data: Dict[str, Any], slot: Dict[str, Any], task: Dict[str, Any]) -> int:
    """到场登记实收量：非负且不能超过本次计划量与剩余量。"""
    actual = int(number(data, "actual_litres", 0))
    upper = min(int(slot["planned_litres"]), int(task["remaining_litres"]))
    if actual > upper:
        raise ValidationError("实收量不能超过%s升" % upper)
    return actual


def settle_arrival(task: Dict[str, Any], actual_litres: int) -> Tuple[str, str]:
    """到场后结算：返回(任务新状态, 待收原因)。未排完转下一时段并保留原因。"""
    task["remaining_litres"] = int(task["remaining_litres"]) - actual_litres
    if task["remaining_litres"] <= 0:
        task["remaining_litres"] = 0
        task["pending_reason"] = ""
        return TASK_SETTLED, ""
    reason = "%s，%s还差%s" % (
        REASON_ARRIVED_SHORT, MEDIA_LABELS[task["medium"]], format_litres(task["remaining_litres"]))
    return TASK_WAITING, reason


def blocking_reasons(tasks: Iterable[Dict[str, Any]]) -> List[str]:
    """离泊前结清闸门：列出所有未结清介质及缺口。"""
    return [
        "%s还差%s" % (MEDIA_LABELS[t["medium"]], format_litres(t["remaining_litres"]))
        for t in tasks
        if t["state"] != TASK_SETTLED and int(t["remaining_litres"]) > 0
    ]
