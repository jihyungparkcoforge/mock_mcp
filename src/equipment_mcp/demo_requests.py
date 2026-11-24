DEMO_REQUESTS = [
    {
        "employee_id": "E001",
        "request": "My laptop is almost 5 years old and really slow. Can I get a replacement?",
        "expected": "approve",  # standard laptop cycle 4y, acquired 2022-01-10
    },
    {
        "employee_id": "E001",
        "request": "I'd like a second monitor for my desk.",
        "expected": "deny",  # standard max 1 monitor, current one from 2025-06-01
    },
    {
        "employee_id": "E003",
        "request": "Can I get a 4K webcam for client calls?",
        "expected": "escalate",  # webcam is not in any policy
    },
    {
        "employee_id": "E003",
        "request": "My laptop is almost 5 years old and really slow. Can I get a replacement?",
        "expected": "deny",  # lying
    },
    {
        "employee_id": "E002",
        "request": "I dropped my laptop and the screen is cracked. I need a new one.",
        "expected": "escalate",  # inside 2y cycle (2025-09-01) but damage reason
    },
    {
        "employee_id": "E002",
        "request": "Could I get another monitor? I want a dual-screen setup.",
        "expected": "deny",  # eligible, but open request ORD-1042 for a monitor exists
    },
    {
        "employee_id": "E007",
        "request": "My laptop is getting old, can I get a new one?",
        "expected": "deny",  # refresh due, but employee is leaving
    },
    {
        "employee_id": "E005",
        "request": "I don't have a monitor yet. Can I get one?",
        "expected": "escalate",  # eligible, but Operations budget 150 < price 300
    },
    {
        "employee_id": "E004",
        "request": "I need a headset for meetings.",
        "expected": "escalate",  # eligible, but headset out of stock
    },
    {
        "employee_id": "E004",
        "request": "Can I get a workstation for model training?",
        "expected": "escalate",  # eligible, but price 3200 > auto-approve limit 2000
    },
    {
        "employee_id": "E006",
        "request": "My laptop is past its refresh date, please replace it.",
        "expected": "escalate",  # refresh due, but employee is on leave
    },
]
