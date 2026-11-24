"""Equipment data and ticket storage boundaries, with JSON and memory implementations."""

import copy
import json
from pathlib import Path
from typing import Any


class JsonEquipmentData:
    def __init__(self, paths: dict[str, Path]):
        self.paths = dict(paths)

    def read(self, table: str) -> dict[str, Any]:
        with self.paths[table].open(encoding="utf-8") as stream:
            return json.load(stream)


class JsonTicketStore:
    def __init__(self, path: Path):
        self.path = path

    def append(self, fields: dict[str, Any]) -> dict[str, Any]:
        existing = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as stream:
                existing = sum(1 for line in stream if line.strip())
        ticket = {"ticket_id": f"ESC-{existing + 1:04d}", **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(ticket, ensure_ascii=False) + "\n")
        return ticket


class MemoryEquipmentData:
    def __init__(self, tables: dict[str, dict[str, Any]]):
        self.tables = copy.deepcopy(tables)

    def read(self, table: str) -> dict[str, Any]:
        return copy.deepcopy(self.tables[table])


class MemoryTicketStore:
    def __init__(self):
        self.tickets: list[dict[str, Any]] = []

    def append(self, fields: dict[str, Any]) -> dict[str, Any]:
        ticket = {"ticket_id": f"ESC-{len(self.tickets) + 1:04d}", **copy.deepcopy(fields)}
        self.tickets.append(ticket)
        return copy.deepcopy(ticket)
