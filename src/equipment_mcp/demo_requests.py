"""The four demo requests for Part 6, with the decision each one should reach."""

DEMO_REQUESTS = [
    {
        "employee_id": "E001",
        "request": "My laptop is almost 5 years old and really slow. Can I get a replacement?",
        "expected": "approve",   # standard laptop cycle 4y, acquired 2022-01-10
    },
    {
        "employee_id": "E001",
        "request": "I'd like a second monitor for my desk.",
        "expected": "deny",      # standard max 1 monitor, current one from 2025-06-01
    },
    {
        "employee_id": "E003",
        "request": "Can I get a 4K webcam for client calls?",
        "expected": "escalate",  # webcam is not in any policy
    },
    {
        "employee_id": "E002",
        "request": "I dropped my laptop and the screen is cracked. I need a new one.",
        "expected": "escalate",  # inside 2y cycle (2025-09-01) but damage reason
    },
]
