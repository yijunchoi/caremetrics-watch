"""Check each guardrail on queries and results we write by hand. No model, so it passes every time.

    uv run python -m evals.check_guardrails
"""

import asyncio
import json
from types import SimpleNamespace

from guardrails import (
    no_identifiers_in_answer,
    no_identifiers_in_query,
    no_identifiers_in_result,
    no_small_groups,
    select_only,
)

ALLOW, REJECT = "allow", "reject"

# (guardrail, the query or result, what it should do)
QUERY_CASES = [
    (select_only, "SELECT count(*) AS visits FROM encounters", ALLOW),
    (select_only, "WITH m AS (SELECT * FROM encounters) SELECT count(*) FROM m", ALLOW),
    (select_only, "SELECT 1; DROP TABLE encounters", REJECT),
    (select_only, "DELETE FROM encounters", REJECT),
    (select_only, "COPY patients TO 'patients.csv'", REJECT),
    (select_only, "ATTACH 'other.db'", REJECT),
    (select_only, "PRAGMA table_info('patients')", REJECT),
    (no_identifiers_in_query, "SELECT first(ENCOUNTERCLASS), last_day(START) FROM encounters ORDER BY 1 NULLS LAST", ALLOW),
    (no_identifiers_in_query, "SELECT count(DISTINCT PATIENT) AS patients FROM encounters", ALLOW),
    (no_identifiers_in_query, "SELECT FIRST, LAST FROM patients", REJECT),
    (no_identifiers_in_query, "SELECT p.ssn FROM patients p", REJECT),
    (no_identifiers_in_query, "SELECT ADDRESS FROM patients", REJECT),
    (no_identifiers_in_query, "SELECT count(*) FROM encounters WHERE PATIENT = 'f0f3bc8d-ef38-49ce-a2bd-dfdda982b271'", REJECT),
]
RESULT_CASES = [
    (no_identifiers_in_result, {"columns": ["insurer", "visits"], "rows": [["Medicare", 2563]]}, ALLOW),
    (no_identifiers_in_result, {"columns": ["x"], "rows": [["Jacinto644 Kris249"]]}, REJECT),
    (no_identifiers_in_result, {"columns": ["x"], "rows": [["999-68-6630"]]}, REJECT),
    (no_identifiers_in_result, {"columns": ["x"], "rows": [["888 Hickle Ferry Suite 38"]]}, REJECT),
    (no_identifiers_in_result, {"columns": ["x"], "rows": [["f0f3bc8d-ef38-49ce-a2bd-dfdda982b271"]]}, REJECT),
    (no_small_groups, {"columns": ["insurer", "visits", "patients"], "rows": [["Medicare", 2563, 1900]]}, ALLOW),
    (no_small_groups, {"columns": ["year", "month_number"], "rows": [[2019, 3]]}, ALLOW),
    (no_small_groups, {"columns": ["insurer", "visits", "patients"], "rows": [["Dual Eligible", 11, 9]]}, REJECT),
    (no_small_groups, {"columns": ["insurer", "visits"], "rows": [["Medicare", 2563]]}, REJECT),  # no patient count
]
ANSWER_CASES = [
    ("Medicare had 2,563 visits in Jan 2020.", ALLOW),
    ("The patient was Jacinto644 Kris249.", REJECT),
]

failures = 0


def report(name: str, case: str, expected: str, got: str):
    global failures
    failures += got != expected
    print(f"  {'ok  ' if got == expected else 'FAIL'} {name:<25} {expected:<6} {case[:80]}")


for guard, query, expected in QUERY_CASES:
    data = SimpleNamespace(context=SimpleNamespace(tool_arguments=json.dumps({"query": query})))
    got = guard.guardrail_function(data).behavior["type"]
    report(guard.name, query, expected, ALLOW if got == "allow" else REJECT)

for guard, result, expected in RESULT_CASES:
    data = SimpleNamespace(context=SimpleNamespace(tool_arguments="{}"), output=json.dumps(result))
    got = guard.guardrail_function(data).behavior["type"]
    report(guard.name, json.dumps(result), expected, ALLOW if got == "allow" else REJECT)

for text, expected in ANSWER_CASES:
    result = asyncio.run(no_identifiers_in_answer.guardrail_function(None, None, text))
    report(no_identifiers_in_answer.name, text, expected, REJECT if result.tripwire_triggered else ALLOW)

print("\nPASS" if not failures else f"\nFAIL: {failures} cases")
