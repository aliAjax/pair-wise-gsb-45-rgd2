"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
                CREATE TABLE IF NOT EXISTS reception_vehicles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vehicle_no TEXT NOT NULL UNIQUE,
                    capacity_liters REAL NOT NULL,
                    media TEXT NOT NULL,
                    dangerous_classes TEXT NOT NULL,
                    slots TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reception_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    medium TEXT NOT NULL,
                    declared_liters REAL NOT NULL,
                    received_liters REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    vehicle_no TEXT NOT NULL DEFAULT '',
                    slot_hour INTEGER,
                    pending_reason TEXT NOT NULL DEFAULT '',
                    shortfall_liters REAL NOT NULL DEFAULT 0,
                    carry_overs TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reception_tasks_record ON reception_tasks(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_reception_tasks_state ON reception_tasks(state);
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

    @staticmethod
    def _vehicle_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["media"] = json.loads(item["media"])
        item["dangerous_classes"] = json.loads(item["dangerous_classes"])
        item["slots"] = json.loads(item["slots"])
        return item

    def create_vehicle(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO reception_vehicles(vehicle_no,capacity_liters,media,dangerous_classes,slots,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        data["vehicle_no"],
                        data["capacity_liters"],
                        json.dumps(data["media"], ensure_ascii=False, sort_keys=True),
                        json.dumps(data["dangerous_classes"], ensure_ascii=False, sort_keys=True),
                        json.dumps(data["slots"]),
                        actor_id,
                        _now(),
                    ),
                )
                row = connection.execute("SELECT * FROM reception_vehicles WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("车辆编号已存在") from exc
        return self._vehicle_row(row)

    def list_vehicles(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reception_vehicles ORDER BY id").fetchall()
        return [self._vehicle_row(row) for row in rows]

    def get_vehicle(self, vehicle_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reception_vehicles WHERE vehicle_no=?", (vehicle_no,)).fetchone()
        if row is None:
            raise NotFound("接收车不存在")
        return self._vehicle_row(row)

    @staticmethod
    def _task_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["carry_overs"] = json.loads(item["carry_overs"])
        return item

    def create_reception_task(self, record_id: int, medium: str, declared_liters: float) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO reception_tasks(record_id,medium,declared_liters,received_liters,state,vehicle_no,slot_hour,pending_reason,shortfall_liters,carry_overs,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, medium, declared_liters, 0.0, "pending", "", None, "待排班", declared_liters, "[]", now, now),
            )
            row = connection.execute("SELECT * FROM reception_tasks WHERE id=?", (int(cursor.lastrowid),)).fetchone()
        return self._task_row(row)

    def get_reception_task(self, task_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reception_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFound("接收任务不存在")
        return self._task_row(row)

    def list_reception_tasks(self, record_id: Optional[int] = None, state: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM reception_tasks"
        clauses, params = [], []
        if record_id is not None:
            clauses.append("record_id=?")
            params.append(record_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._task_row(row) for row in rows]

    def update_reception_task(self, task_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {"state", "vehicle_no", "slot_hour", "received_liters", "pending_reason", "shortfall_liters", "carry_overs"}
        assignments, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "carry_overs":
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            assignments.append("%s=?" % key)
            params.append(value)
        if not assignments:
            return self.get_reception_task(task_id)
        assignments.append("updated_at=?")
        params.append(_now())
        params.append(task_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("UPDATE reception_tasks SET %s WHERE id=?" % ", ".join(assignments), params)
            if cursor.rowcount == 0:
                connection.rollback()
                raise NotFound("接收任务不存在")
            row = connection.execute("SELECT * FROM reception_tasks WHERE id=?", (task_id,)).fetchone()
            connection.commit()
        return self._task_row(row)

    def scheduled_slots(self) -> set:
        with self._connect() as connection:
            rows = connection.execute("SELECT vehicle_no, slot_hour FROM reception_tasks WHERE state='scheduled' AND slot_hour IS NOT NULL").fetchall()
        return {(str(row["vehicle_no"]), int(row["slot_hour"])) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
