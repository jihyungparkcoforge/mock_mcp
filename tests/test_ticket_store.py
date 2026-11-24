import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from equipment_mcp.equipment import flag_for_human_review
from equipment_mcp.repositories import MemoryTicketStore, SqliteTicketStore


def create_ticket(args):
    path, key = args
    return flag_for_human_review(
        "E001",
        "Original\nrequest",
        "Needs review",
        tickets=SqliteTicketStore(Path(path)),
        idempotency_key=key,
    )


@pytest.mark.parametrize("processes", [False, True], ids=["threads", "processes"])
@pytest.mark.parametrize("same_key", [False, True], ids=["different-requests", "retries"])
def test_concurrent_writers_share_one_database(tmp_path, processes, same_key):
    path = tmp_path / "tickets.sqlite3"
    keys = ["same" if same_key else f"request-{i}" for i in range(20)]
    pool = (
        ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
        if processes
        else ThreadPoolExecutor(max_workers=20)
    )
    with pool:
        results = list(pool.map(create_ticket, [(str(path), key) for key in keys], timeout=30))
    expected = 1 if same_key else 20
    assert len({r["ticket_id"] for r in results}) == expected
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == expected
    if same_key:
        assert all(r == results[0] for r in results)
        # A new store/connection after the first writers finish still returns the same ticket.
        assert create_ticket((str(path), "same")) == results[0]


@pytest.mark.parametrize("storage", ["sqlite", "memory"])
def test_retry_preserves_first_ticket_and_rejects_key_reuse(tmp_path, storage):
    store = (
        SqliteTicketStore(tmp_path / "tickets.sqlite3")
        if storage == "sqlite"
        else MemoryTicketStore()
    )
    original = {
        "employee_id": "E001",
        "request": "Laptop",
        "reason": "First",
        "created_at": "first",
    }
    first = store.append(original, idempotency_key="request-1")
    retry = store.append(
        {**original, "reason": "Reworded", "created_at": "later"}, idempotency_key="request-1"
    )
    assert retry == first
    for changes in ({"employee_id": "E002"}, {"request": "Different request"}):
        with pytest.raises(ValueError, match="different employee or request"):
            store.append({**original, **changes}, idempotency_key="request-1")
    second = store.append(original, idempotency_key="request-2")
    assert second["ticket_id"] != first["ticket_id"]


def test_database_constraints_no_key_and_invalid_input(tmp_path):
    path = tmp_path / "tickets.sqlite3"
    store = SqliteTicketStore(path)
    assert not path.exists()
    with pytest.raises(ValueError, match="nonempty"):
        store.append({}, idempotency_key=" ")
    assert not path.exists()
    first = store.append({"request": "one"})
    second = store.append({"request": "one"})
    assert first["ticket_id"] != second["ticket_id"]  # No key means a new operation.
    store.append({"request": "three"}, idempotency_key="third")
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tickets (idempotency_key, payload) VALUES (?, ?)",
                ("third", json.dumps({"request": "other"})),
            )
