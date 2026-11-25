import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from equipment_mcp.equipment import flag_for_human_review
from equipment_mcp.repositories import MemoryEquipmentData, MemoryTicketStore, SqliteTicketStore


def create_ticket(args):
    path, key = args
    return flag_for_human_review(
        "E001",
        "Original\nrequest",
        "Needs review",
        data=MemoryEquipmentData({"employees": {"E001": {}}}),
        tickets=SqliteTicketStore(Path(path)),
        idempotency_key=key,
    )


@pytest.fixture(params=[False, True], ids=["threads", "processes"])
def concurrent_writer(request, tmp_path):
    processes = request.param

    def run(same_key):
        path = tmp_path / "tickets.sqlite3"
        keys = ["same" if same_key else f"request-{i}" for i in range(20)]
        pool = (
            ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
            if processes
            else ThreadPoolExecutor(max_workers=20)
        )
        with pool:
            results = list(pool.map(create_ticket, [(str(path), key) for key in keys], timeout=30))
        return path, results

    return run


@pytest.mark.parametrize("same_key,expected", [(False, 20), (True, 1)], ids=["distinct", "retries"])
def test_concurrent_writers_return_expected_ticket_count(concurrent_writer, same_key, expected):
    _, results = concurrent_writer(same_key)
    assert len({r["ticket_id"] for r in results}) == expected


@pytest.mark.parametrize("same_key,expected", [(False, 20), (True, 1)], ids=["distinct", "retries"])
def test_concurrent_writers_persist_expected_row_count(concurrent_writer, same_key, expected):
    path, _ = concurrent_writer(same_key)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == expected


def test_concurrent_retries_return_identical_payloads(concurrent_writer):
    _, results = concurrent_writer(True)
    assert results == [results[0]] * 20


def test_retry_from_new_connection_returns_original_ticket(concurrent_writer):
    path, results = concurrent_writer(True)
    assert create_ticket((str(path), "same")) == results[0]


@pytest.fixture(params=["sqlite", "memory"])
def ticket_store(request, tmp_path):
    return (
        SqliteTicketStore(tmp_path / "tickets.sqlite3")
        if request.param == "sqlite"
        else MemoryTicketStore()
    )


@pytest.fixture
def original_ticket(ticket_store):
    original = {
        "employee_id": "E001",
        "request": "Laptop",
        "reason": "First",
        "created_at": "first",
    }
    first = ticket_store.append(original, idempotency_key="request-1")
    return original, first


def test_retry_preserves_first_ticket(ticket_store, original_ticket):
    original, first = original_ticket
    retry = ticket_store.append(
        {**original, "reason": "Reworded", "created_at": "later"}, idempotency_key="request-1"
    )
    assert retry == first


@pytest.mark.parametrize(
    "changes",
    [{"employee_id": "E002"}, {"request": "Different request"}],
    ids=["different-employee", "different-request"],
)
def test_retry_rejects_key_reuse(ticket_store, original_ticket, changes):
    original, _ = original_ticket
    with pytest.raises(ValueError, match="different employee or request"):
        ticket_store.append({**original, **changes}, idempotency_key="request-1")


def test_new_key_creates_distinct_ticket(ticket_store, original_ticket):
    original, first = original_ticket
    second = ticket_store.append(original, idempotency_key="request-2")
    assert second["ticket_id"] != first["ticket_id"]


def test_sqlite_construction_is_lazy(tmp_path):
    path = tmp_path / "tickets.sqlite3"
    SqliteTicketStore(path)
    assert not path.exists()


def test_blank_key_is_rejected_before_database_creation(tmp_path):
    path = tmp_path / "tickets.sqlite3"
    store = SqliteTicketStore(path)
    with pytest.raises(ValueError, match="nonempty"):
        store.append({}, idempotency_key=" ")
    assert not path.exists()


def test_missing_key_means_new_operation(tmp_path):
    store = SqliteTicketStore(tmp_path / "tickets.sqlite3")
    first = store.append({"request": "one"})
    second = store.append({"request": "one"})
    assert first["ticket_id"] != second["ticket_id"]


def test_database_enforces_unique_idempotency_key(tmp_path):
    path = tmp_path / "tickets.sqlite3"
    store = SqliteTicketStore(path)
    store.append({"request": "three"}, idempotency_key="third")
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tickets (idempotency_key, payload) VALUES (?, ?)",
                ("third", json.dumps({"request": "other"})),
            )
