"""Unit tests for the plain functions in equipment_mcp.tools (no MCP protocol)."""

import json
from datetime import date, datetime

import pytest

from equipment_mcp.tools import (
    check_request_eligibility,
    flag_for_human_review,
    get_employee_info,
    get_policy_limits,
)

TODAY = date(2026, 11, 23)
NOW = datetime(2026, 11, 23, 10, 0, 0)


# ---------------------------------------------------------------------------
# get_employee_info
# ---------------------------------------------------------------------------

class TestGetEmployeeInfo:
    def test_returns_role_and_profile(self):
        info = get_employee_info("E001", today=TODAY)
        assert info["employee_id"] == "E001"
        assert info["name"] == "Alice Kim"
        assert info["role"] == "standard"
        assert info["hire_date"] == "2021-02-01"

    def test_tenure_in_years_rounded_to_one_decimal(self):
        assert get_employee_info("E001", today=TODAY)["tenure_years"] == 5.8

    def test_new_hire_has_fractional_tenure(self):
        # hired 2026-10-01 -> 53 days
        assert get_employee_info("E003", today=TODAY)["tenure_years"] == 0.1

    def test_returns_current_equipment(self):
        info = get_employee_info("E001", today=TODAY)
        assert info["equipment"] == [
            {"item": "laptop", "acquired": "2022-01-10"},
            {"item": "monitor", "acquired": "2025-06-01"},
        ]

    def test_id_is_normalized(self):
        info = get_employee_info("  e002 ", today=TODAY)
        assert info["employee_id"] == "E002"
        assert info["role"] == "manager"

    def test_unknown_id_returns_error(self):
        assert get_employee_info("E999", today=TODAY) == {
            "error": "employee_not_found",
            "employee_id": "E999",
        }

    def test_empty_id_returns_error(self):
        assert get_employee_info("   ", today=TODAY)["error"] == "employee_not_found"


# ---------------------------------------------------------------------------
# get_policy_limits
# ---------------------------------------------------------------------------

class TestGetPolicyLimits:
    def test_standard_policy(self):
        assert get_policy_limits("standard") == {
            "role": "standard",
            "items": {
                "monitor": {"max_quantity": 1, "refresh_years": 3},
                "laptop": {"max_quantity": 1, "refresh_years": 4},
            },
        }

    def test_manager_policy(self):
        items = get_policy_limits("manager")["items"]
        assert items["monitor"] == {"max_quantity": 2, "refresh_years": 2}
        assert items["laptop"] == {"max_quantity": 1, "refresh_years": 2}

    def test_engineer_policy(self):
        items = get_policy_limits("engineer")["items"]
        assert items["monitor"] == {"max_quantity": 2, "refresh_years": 3}
        assert items["laptop"] == {"max_quantity": 1, "refresh_years": 3}

    def test_role_is_case_insensitive(self):
        assert get_policy_limits("  Manager ")["role"] == "manager"

    def test_unknown_role_returns_error_with_available_roles(self):
        assert get_policy_limits("intern") == {
            "error": "role_not_found",
            "role": "intern",
            "available_roles": ["engineer", "manager", "standard"],
        }

    def test_result_is_a_copy(self):
        first = get_policy_limits("standard")
        first["items"]["laptop"]["refresh_years"] = 99
        assert get_policy_limits("standard")["items"]["laptop"]["refresh_years"] == 4


# ---------------------------------------------------------------------------
# check_request_eligibility
# ---------------------------------------------------------------------------

class TestCheckRequestEligibility:
    def test_refresh_due_is_eligible(self):
        # standard laptop: 4y cycle, acquired 2022-01-10 -> due 2026-01-10
        r = check_request_eligibility("E001", "laptop", today=TODAY)
        assert r["eligible"] is True
        assert r["status"] == "eligible_refresh"
        assert r["currently_held"] == 1
        assert r["max_quantity"] == 1
        assert r["refresh_years"] == 4
        assert r["next_eligible_date"] is None

    def test_at_max_quantity_within_cycle_is_not_eligible(self):
        # standard: max 1 monitor, current one acquired 2025-06-01 -> next 2028-06-01
        r = check_request_eligibility("E001", "monitor", today=TODAY)
        assert r["eligible"] is False
        assert r["status"] == "not_eligible"
        assert r["currently_held"] == 1
        assert r["next_eligible_date"] == "2028-06-01"

    def test_below_max_quantity_is_eligible_new(self):
        # manager: max 2 monitors, holds 1
        r = check_request_eligibility("E002", "monitor", today=TODAY)
        assert r["eligible"] is True
        assert r["status"] == "eligible_new"
        assert r["currently_held"] == 1
        assert r["max_quantity"] == 2

    def test_holding_none_is_eligible_new(self):
        r = check_request_eligibility("E004", "monitor", today=TODAY)
        assert r["status"] == "eligible_new"
        assert r["currently_held"] == 0

    def test_manager_laptop_inside_cycle_is_not_eligible(self):
        # manager laptop: 2y cycle, acquired 2025-09-01 -> next 2027-09-01
        r = check_request_eligibility("E002", "laptop", today=TODAY)
        assert r["eligible"] is False
        assert r["next_eligible_date"] == "2027-09-01"

    def test_refresh_date_exactly_today_is_eligible(self):
        # engineer laptop: 3y cycle, acquired 2023-11-23 -> due 2026-11-23
        r = check_request_eligibility("E004", "laptop", today=TODAY)
        assert r["status"] == "eligible_refresh"

    def test_one_day_before_refresh_date_is_not_eligible(self):
        r = check_request_eligibility("E004", "laptop", today=date(2026, 11, 22))
        assert r["status"] == "not_eligible"
        assert r["next_eligible_date"] == "2026-11-23"

    def test_refresh_uses_oldest_unit(self):
        # engineer holds 2 monitors from 2026-10-01 -> next 2029-10-01
        r = check_request_eligibility("E003", "monitor", today=TODAY)
        assert r["status"] == "not_eligible"
        assert r["currently_held"] == 2
        assert r["next_eligible_date"] == "2029-10-01"

    def test_item_not_in_policy(self):
        r = check_request_eligibility("E003", "webcam", today=TODAY)
        assert r["eligible"] is None
        assert r["status"] == "not_in_policy"
        assert r["item"] == "webcam"

    def test_unknown_employee(self):
        r = check_request_eligibility("E999", "laptop", today=TODAY)
        assert r["eligible"] is None
        assert r["status"] == "employee_not_found"

    def test_inputs_are_normalized(self):
        r = check_request_eligibility(" e001 ", "  Laptop ", today=TODAY)
        assert r["employee_id"] == "E001"
        assert r["item"] == "laptop"
        assert r["status"] == "eligible_refresh"

    @pytest.mark.parametrize(
        "employee_id,item",
        [("E001", "laptop"), ("E001", "monitor"), ("E003", "webcam"), ("E999", "laptop")],
    )
    def test_always_has_reason(self, employee_id, item):
        r = check_request_eligibility(employee_id, item, today=TODAY)
        assert isinstance(r["reason"], str) and r["reason"].strip()


# ---------------------------------------------------------------------------
# flag_for_human_review
# ---------------------------------------------------------------------------

class TestFlagForHumanReview:
    def test_returns_ticket(self, tmp_path):
        log = tmp_path / "escalations.jsonl"
        t = flag_for_human_review(
            "e003", "4K webcam", "Item not in policy", log_path=log, now=NOW
        )
        assert t == {
            "ticket_id": "ESC-0001",
            "status": "pending_review",
            "employee_id": "E003",
            "request": "4K webcam",
            "reason": "Item not in policy",
            "created_at": "2026-11-23T10:00:00",
        }

    def test_writes_ticket_to_log(self, tmp_path):
        log = tmp_path / "escalations.jsonl"
        t = flag_for_human_review("E003", "4K webcam", "Item not in policy",
                                  log_path=log, now=NOW)
        lines = log.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == t

    def test_ticket_ids_are_sequential_and_appended(self, tmp_path):
        log = tmp_path / "escalations.jsonl"
        flag_for_human_review("E003", "webcam", "not in policy", log_path=log, now=NOW)
        second = flag_for_human_review("E002", "laptop", "cracked screen",
                                       log_path=log, now=NOW)
        assert second["ticket_id"] == "ESC-0002"
        assert len(log.read_text().splitlines()) == 2

    def test_blank_reason_is_rejected_and_not_written(self, tmp_path):
        log = tmp_path / "escalations.jsonl"
        assert flag_for_human_review("E003", "webcam", "  ", log_path=log, now=NOW) == {
            "error": "reason_required"
        }
        assert not log.exists() or log.read_text() == ""

    def test_blank_request_is_rejected_and_not_written(self, tmp_path):
        log = tmp_path / "escalations.jsonl"
        assert flag_for_human_review("E003", "", "why", log_path=log, now=NOW) == {
            "error": "request_required"
        }
        assert not log.exists() or log.read_text() == ""
