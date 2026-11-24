"""JSON equipment data, SQLite tickets, and memory stores for isolated tests."""

import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock
from typing import Any


class JsonEquipmentData:
    def __init__(self, paths: dict[str, Path]):
        self.paths = dict(paths)

    def read(self, table: str) -> dict[str, Any]:
        with self.paths[table].open(encoding="utf-8") as stream:
            return json.load(stream)


def check_retry(existing: dict[str, Any], fields: dict[str, Any]) -> None:
    # Reason/timestamp can change on a model retry. The first stored version wins.
    if any(existing.get(k) != fields.get(k) for k in ("employee_id", "request")):
        raise ValueError("Idempotency key already belongs to a different employee or request")


def check_key(key: str | None) -> None:
    if key is not None and (not isinstance(key, str) or not key.strip()):
        raise ValueError("Idempotency key must be a nonempty string")


class SqliteTicketStore:
    """Atomic ID allocation and retry deduplication across connections/processes."""

    def __init__(self, path: Path):
        self.path = path

    def append(
        self, fields: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        check_key(idempotency_key)
        payload = json.dumps(fields, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS tickets ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "idempotency_key TEXT UNIQUE, payload TEXT NOT NULL)"
                )
                row = connection.execute(
                    "SELECT id, payload FROM tickets WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    stored = json.loads(row[1])
                    check_retry(stored, fields)
                    ticket = {**stored, "ticket_id": f"ESC-{row[0]:04d}"}
                else:
                    cursor = connection.execute(
                        "INSERT INTO tickets (idempotency_key, payload) VALUES (?, ?)",
                        (idempotency_key, payload),
                    )
                    ticket = {**fields, "ticket_id": f"ESC-{cursor.lastrowid:04d}"}
            # Each call owns its connection and returns only after commit succeeds.
            return ticket


class MemoryEquipmentData:
    def __init__(self, tables: dict[str, dict[str, Any]]):
        self.tables = copy.deepcopy(tables)

    def read(self, table: str) -> dict[str, Any]:
        return copy.deepcopy(self.tables[table])


class MemoryTicketStore:
    def __init__(self):
        self.tickets: list[dict[str, Any]] = []
        self._keys: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    def append(
        self, fields: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        check_key(idempotency_key)
        with self._lock:
            if idempotency_key in self._keys:
                existing = self._keys[idempotency_key]
                check_retry(existing, fields)
                return copy.deepcopy(existing)
            ticket = {**copy.deepcopy(fields), "ticket_id": f"ESC-{len(self.tickets) + 1:04d}"}
            self.tickets.append(ticket)
            if idempotency_key is not None:
                self._keys[idempotency_key] = ticket
            return copy.deepcopy(ticket)
