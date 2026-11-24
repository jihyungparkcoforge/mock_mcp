"""CLI: choose --demo or --interactive to submit equipment requests."""

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

from equipment_mcp.app import run_agent
from equipment_mcp.demo_requests import DEMO_REQUESTS
from equipment_mcp.formatting import format_trace_step
from equipment_mcp.models import AgentResult


def print_result(result: AgentResult) -> None:
    color = (
        sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    )
    for step in result.trace:
        print(format_trace_step(step, color=color))
    print("-" * 78)
    for decision in result.decisions:
        print(f"REQUEST  : {decision.request_id} | {decision.item}")
        print(f"DECISION : {decision.decision}")
        if decision.ticket_id:
            print(f"TICKET   : {decision.ticket_id}")
        print(f"RESPONSE : {decision.response}")
    verdicts = ", ".join(r["verdict"] for r in result.reflections)
    print(f"REFLECT  : {verdicts}")


async def run_demos() -> None:
    correct = 0
    mismatches = []
    for i, demo in enumerate(DEMO_REQUESTS, start=1):
        print("=" * 78)
        print(f"Request {i} | employee {demo['employee_id']} | expected: {demo['expected']}")
        print(f"> {demo['request']}")
        print("-" * 78)
        result = await run_agent(demo["request"], demo["employee_id"])
        print_result(result)
        matched = len(result.decisions) == 1 and result.decision == demo["expected"]
        if matched:
            correct += 1
        else:
            actual = ", ".join(d.decision for d in result.decisions) or "no decision"
            mismatches.append(
                f"  #{i} | {demo['employee_id']} | expected: {demo['expected']} | actual: {actual}"
            )
        print(f"CHECK    : {'OK' if matched else 'MISMATCH'}")
        print()

    total = len(DEMO_REQUESTS)
    accuracy = f"{correct / total:.1%}" if total else "N/A"
    print("=" * 78)
    print("DEMO SUMMARY")
    print("-" * 78)
    print(f"Total: {total}  |  Correct: {correct}  |  Mismatched: {len(mismatches)}")
    print(f"Decision accuracy: {accuracy} ({correct}/{total})")
    if mismatches:
        print("Mismatched requests:")
        print("\n".join(mismatches))
    print("Scores decision labels only (approve / deny / escalate).")
    print("=" * 78)


def read_required(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Please enter a value.")


async def run_interactive() -> None:
    employee_id = read_required("Employee ID (e.g. E001): ")
    request = read_required("Equipment request: ")
    print("-" * 78)
    result = await run_agent(request, employee_id)
    print_result(result)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the equipment request agent.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="Run all predefined demo requests.")
    mode.add_argument(
        "--interactive", action="store_true", help="Enter an employee ID and a request."
    )
    args = parser.parse_args()
    load_dotenv()
    try:
        asyncio.run(run_demos() if args.demo else run_interactive())
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")


if __name__ == "__main__":
    main()
