import json
import sqlite3
from datetime import date, datetime

import pytest

from equipment_mcp.equipment import (
    check_inventory,
    check_request_eligibility,
    flag_for_human_review,
    get_department_budget,
    get_employee_info,
    get_item_catalog,
    get_open_requests,
    get_policy_limits,
)
from equipment_mcp.repositories import MemoryEquipmentData, SqliteTicketStore
from equipment_mcp.storage import default_data

TODAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 23, 10, 0, 0)


@pytest.fixture
def data():
    return default_data()


class TestGetEmployeeInfo:
    def test_returns_role_and_profile(self, data):
        info = get_employee_info("E001", today=TODAY, data=data)
        assert info["employee_id"] == "E001"
        assert info["name"] == "Alice Kim"
        assert info["role"] == "standard"
        assert info["hire_date"] == "2021-02-01"

    def test_tenure_in_years_rounded_to_one_decimal(self, data):
        assert get_employee_info("E001", today=TODAY, data=data)["tenure_years"] == 5.8

    def test_new_hire_has_fractional_tenure(self, data):
        # hired 2026-10-01 -> 53 days
        assert get_employee_info("E003", today=TODAY, data=data)["tenure_years"] == 0.1

    def test_returns_current_equipment(self, data):
        info = get_employee_info("E001", today=TODAY, data=data)
        assert info["equipment"] == [
            {"item": "laptop", "acquired": "2022-01-10"},
            {"item": "monitor", "acquired": "2025-06-01"},
        ]

    def test_id_is_normalized(self, data):
        info = get_employee_info("  e002 ", today=TODAY, data=data)
        assert info["employee_id"] == "E002"
        assert info["role"] == "manager"

    def test_unknown_id_returns_error(self, data):
        assert get_employee_info("E999", today=TODAY, data=data) == {
            "error": "employee_not_found",
            "employee_id": "E999",
        }

    def test_empty_id_returns_error(self, data):
        assert get_employee_info("   ", today=TODAY, data=data)["error"] == "employee_not_found"


class TestGetPolicyLimits:
    def test_standard_policy(self, data):
        assert get_policy_limits("standard", data=data) == {
            "role": "standard",
            "items": {
                "monitor": {"max_quantity": 1, "refresh_years": 3},
                "laptop": {"max_quantity": 1, "refresh_years": 4},
                "headset": {"max_quantity": 1, "refresh_years": 2},
            },
        }

    def test_manager_policy(self, data):
        items = get_policy_limits("manager", data=data)["items"]
        assert items["monitor"] == {"max_quantity": 2, "refresh_years": 2}
        assert items["laptop"] == {"max_quantity": 1, "refresh_years": 2}

    def test_engineer_policy(self, data):
        items = get_policy_limits("engineer", data=data)["items"]
        assert items["monitor"] == {"max_quantity": 2, "refresh_years": 3}
        assert items["laptop"] == {"max_quantity": 1, "refresh_years": 3}

    def test_role_is_case_insensitive(self, data):
        assert get_policy_limits("  Manager ", data=data)["role"] == "manager"

    def test_unknown_role_returns_error_with_available_roles(self, data):
        assert get_policy_limits("intern", data=data) == {
            "error": "role_not_found",
            "role": "intern",
            "available_roles": ["engineer", "manager", "standard"],
        }

    def test_result_is_a_copy(self):
        policies = {"standard": {"laptop": {"max_quantity": 1, "refresh_years": 4}}}

        class SharedData:
            def read(self, table):
                assert table == "policies"
                return policies  # No storage-layer copy to hide a missing business-layer copy.

        data = SharedData()
        first = get_policy_limits("standard", data=data)
        first["items"]["laptop"]["refresh_years"] = 99
        assert policies["standard"]["laptop"]["refresh_years"] == 4
        assert get_policy_limits("standard", data=data)["items"]["laptop"]["refresh_years"] == 4


class TestCheckRequestEligibility:
    def test_refresh_due_is_eligible(self, data):
        # standard laptop: 4y cycle, acquired 2022-01-10 -> due 2026-01-10
        r = check_request_eligibility("E001", "laptop", today=TODAY, data=data)
        assert r["eligible"] is True
        assert r["status"] == "eligible_refresh"
        assert r["currently_held"] == 1
        assert r["max_quantity"] == 1
        assert r["refresh_years"] == 4
        assert r["next_eligible_date"] is None

    def test_at_max_quantity_within_cycle_is_not_eligible(self, data):
        # standard: max 1 monitor, current one acquired 2025-06-01 -> next 2028-06-01
        r = check_request_eligibility("E001", "monitor", today=TODAY, data=data)
        assert r["eligible"] is False
        assert r["status"] == "not_eligible"
        assert r["currently_held"] == 1
        assert r["next_eligible_date"] == "2028-06-01"

    def test_below_max_quantity_is_eligible_new(self, data):
        # manager: max 2 monitors, holds 1
        r = check_request_eligibility("E002", "monitor", today=TODAY, data=data)
        assert r["eligible"] is True
        assert r["status"] == "eligible_new"
        assert r["currently_held"] == 1
        assert r["max_quantity"] == 2

    def test_holding_none_is_eligible_new(self, data):
        r = check_request_eligibility("E004", "monitor", today=TODAY, data=data)
        assert r["status"] == "eligible_new"
        assert r["currently_held"] == 0

    def test_manager_laptop_inside_cycle_is_not_eligible(self, data):
        # manager laptop: 2y cycle, acquired 2025-09-01 -> next 2027-09-01
        r = check_request_eligibility("E002", "laptop", today=TODAY, data=data)
        assert r["eligible"] is False
        assert r["next_eligible_date"] == "2027-09-01"

    def test_refresh_date_exactly_today_is_eligible(self, data):
        # engineer laptop: 3y cycle, acquired 2023-11-23 -> due 2026-11-23
        r = check_request_eligibility("E004", "laptop", today=TODAY, data=data)
        assert r["status"] == "eligible_refresh"

    def test_one_day_before_refresh_date_is_not_eligible(self, data):
        r = check_request_eligibility("E004", "laptop", today=date(2026, 11, 22), data=data)
        assert r["status"] == "not_eligible"
        assert r["next_eligible_date"] == "2026-11-23"

    @pytest.mark.parametrize(
        "dates",
        [
            ("2026-10-01", "2023-11-23"),
            ("2023-11-23", "2026-10-01"),
        ],
    )
    def test_refresh_uses_oldest_unit(self, dates):
        data = MemoryEquipmentData(
            {
                "employees": {
                    "E001": {
                        "role": "engineer",
                        "equipment": [
                            {"item": "monitor", "acquired": acquired} for acquired in dates
                        ],
                    }
                },
                "policies": {"engineer": {"monitor": {"max_quantity": 2, "refresh_years": 3}}},
            }
        )
        # Only the older monitor is due; list order must not affect eligibility.
        r = check_request_eligibility("E001", "monitor", today=TODAY, data=data)
        assert r["status"] == "eligible_refresh"
        assert r["eligible"] is True
        assert r["currently_held"] == 2

    @pytest.mark.parametrize(
        "today, status, due",
        [
            (date(2025, 2, 27), "not_eligible", "2025-02-28"),
            (date(2025, 2, 28), "eligible_refresh", None),
        ],
    )
    def test_leap_day_refresh_in_non_leap_year(self, today, status, due):
        data = MemoryEquipmentData(
            {
                "employees": {
                    "E001": {
                        "role": "standard",
                        "equipment": [{"item": "laptop", "acquired": "2024-02-29"}],
                    }
                },
                "policies": {"standard": {"laptop": {"max_quantity": 1, "refresh_years": 1}}},
            }
        )
        result = check_request_eligibility("E001", "laptop", today=today, data=data)
        assert result["status"] == status
        assert result["eligible"] is (status == "eligible_refresh")
        assert result["next_eligible_date"] == due

    def test_employee_role_without_policy(self):
        data = MemoryEquipmentData(
            {
                "employees": {"E001": {"role": "contractor", "equipment": []}},
                "policies": {},
            }
        )
        result = check_request_eligibility("E001", "laptop", today=TODAY, data=data)
        assert result["employee_id"] == "E001"
        assert result["item"] == "laptop"
        assert result["status"] == "role_not_found"
        assert result["eligible"] is None
        assert "contractor" in result["reason"]

    def test_item_not_in_policy(self, data):
        r = check_request_eligibility("E003", "webcam", today=TODAY, data=data)
        assert r["eligible"] is None
        assert r["status"] == "not_in_policy"
        assert r["item"] == "webcam"

    def test_unknown_employee(self, data):
        r = check_request_eligibility("E999", "laptop", today=TODAY, data=data)
        assert r["eligible"] is None
        assert r["status"] == "employee_not_found"

    def test_inputs_are_normalized(self, data):
        r = check_request_eligibility(" e001 ", "  Laptop ", today=TODAY, data=data)
        assert r["employee_id"] == "E001"
        assert r["item"] == "laptop"
        assert r["status"] == "eligible_refresh"

    @pytest.mark.parametrize(
        "employee_id,item",
        [("E001", "laptop"), ("E001", "monitor"), ("E003", "webcam"), ("E999", "laptop")],
    )
    def test_always_has_reason(self, employee_id, item, data):
        r = check_request_eligibility(employee_id, item, today=TODAY, data=data)
        assert isinstance(r["reason"], str) and r["reason"].strip()


class TestFlagForHumanReview:
    @pytest.mark.parametrize("employee_id", ["E999", " e999 ", "   "])
    @pytest.mark.parametrize("existing_database", [False, True])
    def test_unknown_employee_is_rejected_without_writing(
        self, tmp_path, employee_id, existing_database
    ):
        data = MemoryEquipmentData({"employees": {"E001": {}}})
        log = tmp_path / "tickets.sqlite3"
        tickets = SqliteTicketStore(log)
        before = []
        if existing_database:
            flag_for_human_review(
                "E001", "Laptop", "Needs review", data=data, tickets=tickets, now=NOW
            )
            with sqlite3.connect(log) as connection:
                before = connection.execute("SELECT id, payload FROM tickets").fetchall()

        assert flag_for_human_review(
            employee_id, "Laptop", "Needs review", data=data, tickets=tickets, now=NOW
        ) == {"error": "employee_not_found", "employee_id": employee_id.strip().upper()}

        if existing_database:
            with sqlite3.connect(log) as connection:
                assert connection.execute("SELECT id, payload FROM tickets").fetchall() == before
        else:
            assert not log.exists()

    def test_returns_ticket(self, tmp_path, data):
        log = tmp_path / "tickets.sqlite3"
        t = flag_for_human_review(
            "e003",
            "4K webcam",
            "Item not in policy",
            data=data,
            tickets=SqliteTicketStore(log),
            now=NOW,
        )
        assert t == {
            "ticket_id": "ESC-0001",
            "status": "pending_review",
            "employee_id": "E003",
            "request": "4K webcam",
            "reason": "Item not in policy",
            "created_at": "2026-11-23T10:00:00",
        }

    def test_writes_ticket_to_database(self, tmp_path, data):
        log = tmp_path / "tickets.sqlite3"
        t = flag_for_human_review(
            "E003",
            "4K webcam",
            "Item not in policy",
            data=data,
            tickets=SqliteTicketStore(log),
            now=NOW,
        )
        with sqlite3.connect(log) as connection:
            rows = connection.execute("SELECT id, payload FROM tickets").fetchall()
        assert len(rows) == 1
        assert {**json.loads(rows[0][1]), "ticket_id": f"ESC-{rows[0][0]:04d}"} == t

    def test_ticket_ids_are_sequential_and_appended(self, tmp_path, data):
        log = tmp_path / "tickets.sqlite3"
        flag_for_human_review(
            "E003", "webcam", "not in policy", data=data, tickets=SqliteTicketStore(log), now=NOW
        )
        second = flag_for_human_review(
            "E002", "laptop", "cracked screen", data=data, tickets=SqliteTicketStore(log), now=NOW
        )
        assert second["ticket_id"] == "ESC-0002"
        with sqlite3.connect(log) as connection:
            assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2

    def test_blank_reason_is_rejected_and_not_written(self, tmp_path, data):
        log = tmp_path / "tickets.sqlite3"
        assert flag_for_human_review(
            "E003", "webcam", "  ", data=data, tickets=SqliteTicketStore(log), now=NOW
        ) == {"error": "reason_required"}
        assert not log.exists()

    def test_blank_request_is_rejected_and_not_written(self, tmp_path, data):
        log = tmp_path / "tickets.sqlite3"
        assert flag_for_human_review(
            "E003", "", "why", data=data, tickets=SqliteTicketStore(log), now=NOW
        ) == {"error": "request_required"}
        assert not log.exists()


class TestEmployeeStatusFields:
    def test_includes_department_and_status(self, data):
        info = get_employee_info("E001", today=TODAY, data=data)
        assert info["department"] == "Finance"
        assert info["employment_status"] == "active"

    @pytest.mark.parametrize("eid,status", [("E006", "on_leave"), ("E007", "leaving")])
    def test_non_active_statuses(self, eid, status, data):
        assert get_employee_info(eid, today=TODAY, data=data)["employment_status"] == status


class TestGetItemCatalog:
    def test_returns_price_and_limit(self, data):
        assert get_item_catalog("laptop", data=data) == {
            "item": "laptop",
            "price": 1400,
            "currency": "USD",
            "auto_approve_limit": 2000,
        }

    def test_item_is_normalized(self, data):
        assert get_item_catalog("  Workstation ", data=data)["price"] == 3200

    def test_unknown_item(self, data):
        assert get_item_catalog("webcam", data=data) == {
            "error": "item_not_in_catalog",
            "item": "webcam",
        }


class TestGetDepartmentBudget:
    def test_returns_remaining(self, data):
        assert get_department_budget("Finance", data=data) == {
            "department": "Finance",
            "annual_budget": 20000,
            "spent": 8200,
            "remaining": 11800,
        }

    def test_name_is_case_insensitive(self, data):
        assert get_department_budget(" operations ", data=data)["remaining"] == 150

    def test_unknown_department(self, data):
        assert get_department_budget("Legal", data=data) == {
            "error": "department_not_found",
            "department": "Legal",
            "available_departments": ["Engineering", "Finance", "Operations", "Sales"],
        }


class TestCheckInventory:
    def test_in_stock(self, data):
        assert check_inventory("monitor", data=data) == {
            "item": "monitor",
            "in_stock": 14,
            "restock_date": None,
        }

    def test_out_of_stock_has_restock_date(self, data):
        r = check_inventory("Headset", data=data)
        assert r["in_stock"] == 0
        assert r["restock_date"] == "2026-12-15"

    def test_unknown_item(self, data):
        assert check_inventory("webcam", data=data) == {
            "error": "item_not_in_inventory",
            "item": "webcam",
        }


class TestGetOpenRequests:
    def test_lists_open_requests(self, data):
        assert get_open_requests("e002", data=data) == {
            "employee_id": "E002",
            "open_requests": [
                {
                    "request_id": "ORD-1042",
                    "item": "monitor",
                    "status": "awaiting_delivery",
                    "created": "2026-11-10",
                }
            ],
        }

    def test_employee_without_open_requests(self, data):
        assert get_open_requests("E001", data=data) == {"employee_id": "E001", "open_requests": []}

    def test_unknown_employee(self, data):
        assert get_open_requests("E999", data=data) == {
            "error": "employee_not_found",
            "employee_id": "E999",
        }
