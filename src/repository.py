"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);

                CREATE TABLE IF NOT EXISTS reception_vehicles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plate TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reception_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    medium TEXT NOT NULL,
                    demand_litres INTEGER NOT NULL,
                    received_litres INTEGER NOT NULL DEFAULT 0,
                    remaining_litres INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    pending_reason TEXT NOT NULL DEFAULT '',
                    skip_until_hour INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(record_id, medium)
                );
                CREATE TABLE IF NOT EXISTS reception_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES reception_tasks(id) ON DELETE CASCADE,
                    vehicle_id INTEGER NOT NULL REFERENCES reception_vehicles(id),
                    start_hour INTEGER NOT NULL,
                    end_hour INTEGER NOT NULL,
                    planned_litres INTEGER NOT NULL,
                    actual_litres INTEGER,
                    state TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    arrived_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_record ON reception_tasks(record_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_state ON reception_tasks(state);
                CREATE INDEX IF NOT EXISTS idx_slots_task ON reception_slots(task_id);
                CREATE INDEX IF NOT EXISTS idx_slots_vehicle ON reception_slots(vehicle_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ---- 污染物接收 ----

    @staticmethod
    def _vehicle_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        payload = json.loads(item["payload"])
        item.update(payload)
        item["payload"] = payload
        item["active"] = bool(item["active"])
        return item

    @staticmethod
    def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _load_vehicles(self, connection: sqlite3.Connection, only_active: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM reception_vehicles"
        if only_active:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        return [self._vehicle_row(row) for row in connection.execute(sql).fetchall()]

    def _load_slots(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        return [dict(row) for row in connection.execute("SELECT * FROM reception_slots ORDER BY id").fetchall()]

    def _load_tasks(self, connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        return [self._task_row(row) for row in connection.execute("SELECT * FROM reception_tasks ORDER BY id").fetchall()]

    def create_vehicle(self, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO reception_vehicles(plate,payload,active,created_by,created_at) VALUES(?,?,1,?,?)",
                    (payload["plate"], json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now),
                )
                row = connection.execute("SELECT * FROM reception_vehicles WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("车牌号已登记") from exc
        return self._vehicle_row(row)

    def list_vehicles(self, active: bool = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if active is None:
                rows = connection.execute("SELECT * FROM reception_vehicles ORDER BY id").fetchall()
            else:
                rows = connection.execute("SELECT * FROM reception_vehicles WHERE active=? ORDER BY id", (1 if active else 0,)).fetchall()
        return [self._vehicle_row(row) for row in rows]

    def get_vehicle(self, vehicle_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reception_vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if row is None:
            raise NotFound("接收车不存在")
        return self._vehicle_row(row)

    def set_vehicle_active(self, vehicle_id: int, active: bool) -> Dict[str, Any]:
        with self._connect() as connection:
            result = connection.execute("UPDATE reception_vehicles SET active=? WHERE id=?", (1 if active else 0, vehicle_id))
            if result.rowcount == 0:
                raise NotFound("接收车不存在")
            row = connection.execute("SELECT * FROM reception_vehicles WHERE id=?", (vehicle_id,)).fetchone()
        return self._vehicle_row(row)

    def ensure_tasks_for_record(self, record: Dict[str, Any], demands: Dict[str, int], actor_id: str) -> List[Dict[str, Any]]:
        """按靠泊申报的两种污水量创建接收任务（已存在或零需求不创建）。"""
        now = _now()
        with self._connect() as connection:
            for medium, demand in demands.items():
                existing = connection.execute(
                    "SELECT id FROM reception_tasks WHERE record_id=? AND medium=?", (record["id"], medium)
                ).fetchone()
                if existing or demand <= 0:
                    continue
                connection.execute(
                    "INSERT INTO reception_tasks(record_id,medium,demand_litres,received_litres,remaining_litres,"
                    "state,pending_reason,created_by,created_at,updated_at) VALUES(?,?,?,0,?,?,'',?,?,?)",
                    (record["id"], medium, demand, demand, "waiting", actor_id, now, now),
                )
            rows = connection.execute("SELECT * FROM reception_tasks WHERE record_id=? ORDER BY id", (record["id"],)).fetchall()
        return [self._task_row(row) for row in rows]

    def list_tasks(self, state: Optional[str] = None, record_id: Optional[int] = None) -> List[Dict[str, Any]]:
        clauses = []
        params: List[Any] = []
        if state:
            clauses.append("state=?")
            params.append(state)
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        sql = "SELECT * FROM reception_tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._task_row(row) for row in rows]

    def get_task(self, task_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reception_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("接收任务不存在")
        return self._task_row(row)

    def list_slots(self, task_id: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if task_id is None:
                rows = connection.execute("SELECT * FROM reception_slots ORDER BY id").fetchall()
            else:
                rows = connection.execute("SELECT * FROM reception_slots WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_slot(self, slot_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reception_slots WHERE id=?", (slot_id,)).fetchone()
        if row is None:
            raise NotFound("排班时段不存在")
        return dict(row)

    def run_scheduling(self, planner, actor_id: str) -> List[Dict[str, Any]]:
        """在单一一致事务内执行planner(tasks, vehicles, payloads)。

        planner为service注入的判定回调，返回写入操作列表：
          ("slot", task_id, plan)
          ("task", task_id, state, pending_reason)
        提交后返回最新任务列表。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tasks = self._load_tasks(connection)
            slots = self._load_slots(connection)
            vehicles = self._load_vehicles(connection)
            for vehicle in vehicles:
                vehicle["slots"] = slots
            payload_rows = connection.execute("SELECT id, payload FROM records").fetchall()
            payloads = {int(row["id"]): json.loads(row["payload"]) for row in payload_rows}
            operations = planner(tasks, vehicles, payloads)
            for operation in operations:
                if operation[0] == "slot":
                    _, task_id, plan = operation
                    connection.execute(
                        "INSERT INTO reception_slots(task_id,vehicle_id,start_hour,end_hour,planned_litres,"
                        "actual_litres,state,created_by,created_at) VALUES(?,?,?,?,?,NULL,?,?,?)",
                        (task_id, plan["vehicle_id"], plan["start_hour"], plan["end_hour"],
                         plan["planned_litres"], "assigned", actor_id, now),
                    )
                elif operation[0] == "task":
                    _, task_id, state, pending_reason = operation
                    skip_until = operation[4] if len(operation) > 4 else None
                    if skip_until is None:
                        connection.execute(
                            "UPDATE reception_tasks SET state=?,pending_reason=?,updated_at=? WHERE id=?",
                            (state, pending_reason, now, task_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE reception_tasks SET state=?,pending_reason=?,skip_until_hour=?,updated_at=? WHERE id=?",
                            (state, pending_reason, int(skip_until), now, task_id),
                        )
            result = self._load_tasks(connection)
            connection.commit()
        return result

    def arrive(self, slot_id: int, actual_litres: int, task_state: str, pending_reason: str,
               remaining_litres: int, received_litres: int, skip_until_hour: Optional[int]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            slot_row = connection.execute("SELECT * FROM reception_slots WHERE id=?", (slot_id,)).fetchone()
            if slot_row is None:
                connection.rollback()
                raise NotFound("排班时段不存在")
            slot = dict(slot_row)
            if slot["state"] != "assigned":
                connection.rollback()
                raise Conflict("该排班已登记到场")
            slot_state = "arrived" if task_state == "settled" or actual_litres >= int(slot["planned_litres"]) else "incomplete"
            connection.execute(
                "UPDATE reception_slots SET actual_litres=?,state=?,arrived_at=? WHERE id=?",
                (actual_litres, slot_state, now, slot_id),
            )
            if skip_until_hour is None:
                connection.execute(
                    "UPDATE reception_tasks SET state=?,pending_reason=?,remaining_litres=?,received_litres=?,updated_at=? WHERE id=?",
                    (task_state, pending_reason, remaining_litres, received_litres, now, slot["task_id"]),
                )
            else:
                connection.execute(
                    "UPDATE reception_tasks SET state=?,pending_reason=?,remaining_litres=?,received_litres=?,"
                    "skip_until_hour=?,updated_at=? WHERE id=?",
                    (task_state, pending_reason, remaining_litres, received_litres, int(skip_until_hour), now, slot["task_id"]),
                )
            new_slot = dict(connection.execute("SELECT * FROM reception_slots WHERE id=?", (slot_id,)).fetchone())
            new_task = self._task_row(connection.execute("SELECT * FROM reception_tasks WHERE id=?", (slot["task_id"],)).fetchone())
            connection.commit()
        return new_slot, new_task

    def reception_stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM reception_tasks GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}


    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
