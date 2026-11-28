"""Guardrails: privacy and safety rules in code, around the model, so no prompt can talk past them.

On run_sql (tools/sql.py), before the query runs:
- select_only: one SELECT statement, nothing else.
- no_identifiers_in_query: no name, SSN, address, licence or passport columns; no patient or visit Id.
On run_sql, after it runs:
- no_identifiers_in_result: no patient names, SSNs, addresses or Ids in the rows.
- no_small_groups: no count of 1 to 10 patients or visits: groups that small could single someone out.
On the answer the user sees (Desk and Analyst):
- no_identifiers_in_answer: the same identifier check. Tripping it stops the run.
A rejected tool call goes back to the model as the tool's answer, with the reason, so it can try
again within the rules.
"""

import json
import re

import duckdb
from agents import (
    GuardrailFunctionOutput,
    ToolGuardrailFunctionOutput,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)

from tools.db import db

SMALLEST_GROUP = 11

# --- What identifies a patient ---

# The real values, to look for in results and answers. Synthea names carry digits ("Jacinto644"),
# so matching them exactly doesn't block ordinary words.
with db.cursor() as cursor:
    _people = cursor.execute("SELECT FIRST, LAST, DRIVERS, PASSPORT, ADDRESS FROM patients").fetchall()
NAMES_AND_DOCUMENTS = {value for row in _people for value in row[:4] if value}
ADDRESSES = {row[4] for row in _people if row[4]}

ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{32}\b")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def find_identifiers(text: str) -> list[str]:
    """The kinds of patient identifier in a piece of text, e.g. ['name', 'SSN']."""
    found = []
    if ID.search(text):
        found.append("ID")
    if SSN.search(text):
        found.append("SSN")
    if NAMES_AND_DOCUMENTS & set(re.findall(r"[A-Za-z0-9']+", text)):
        found.append("name or licence/passport number")
    if any(address in text for address in ADDRESSES):
        found.append("address")
    return found


# Columns that are only ever useful for finding a person.
IDENTIFYING_COLUMNS = re.compile(
    r"\b(ssn|drivers|passport|prefix|suffix|maiden|birthplace|address|zip|lat|lon)\b"
    # FIRST and LAST are the name columns, but also SQL words: allow "NULLS FIRST" and first(...).
    r"|(?<!nulls )\b(first|last)\b(?!\s*\()"
)


def _query(data) -> str:
    return json.loads(data.context.tool_arguments or "{}").get("query", "")


# --- On the query ---


@tool_input_guardrail
def select_only(data) -> ToolGuardrailFunctionOutput:
    """Allow exactly one SELECT statement. DuckDB's own parser decides what each statement is."""
    try:
        statements = duckdb.extract_statements(_query(data))
    except duckdb.Error as e:
        return ToolGuardrailFunctionOutput.reject_content(f"Rejected: the query doesn't parse: {e}")
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        kinds = ", ".join(s.type.name for s in statements)
        return ToolGuardrailFunctionOutput.reject_content(
            f"Rejected: only one SELECT statement is allowed (got: {kinds or 'nothing'})."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_input_guardrail
def no_identifiers_in_query(data) -> ToolGuardrailFunctionOutput:
    """Reject queries that touch identifying columns, or name one patient or visit by its Id."""
    query = " ".join(_query(data).lower().split())  # one space between words, for the NULLS check
    column = IDENTIFYING_COLUMNS.search(query)
    if column:
        return ToolGuardrailFunctionOutput.reject_content(
            f"Rejected: '{column.group()}' identifies patients. Use only totals, counts and shares."
        )
    if ID.search(query):
        return ToolGuardrailFunctionOutput.reject_content(
            "Rejected: the query names a single patient or visit by its Id. Ask about groups instead."
        )
    return ToolGuardrailFunctionOutput.allow()


# --- On the result ---


@tool_output_guardrail
def no_identifiers_in_result(data) -> ToolGuardrailFunctionOutput:
    """Reject results that contain a patient's name, SSN, address or Id."""
    found = find_identifiers(str(data.output))
    if found:
        return ToolGuardrailFunctionOutput.reject_content(
            f"Rejected: the result contains patient identifiers ({', '.join(found)}). "
            "Return only totals, counts and shares."
        )
    return ToolGuardrailFunctionOutput.allow()


# Columns that count people or visits, by name: patients, visits, n, num_admissions, count_star()...
COUNT_COLUMN = re.compile(r"patient|visit|encounter|admission|case|count|^n$|^num")


@tool_output_guardrail
def no_small_groups(data) -> ToolGuardrailFunctionOutput:
    """Reject results with a group of 1 to 10 patients or visits.

    Fewer than 11 visits always means fewer than 11 patients. But 11 visits can be 9 patients, so
    a visit count must come with a patient count beside it.
    """
    try:
        result = json.loads(data.output)
    except (TypeError, json.JSONDecodeError):
        return ToolGuardrailFunctionOutput.allow()
    columns = [column.lower() for column in result.get("columns", [])]
    counts = [column for column in columns if COUNT_COLUMN.search(column)]
    if counts and not any("patient" in column for column in counts):
        return ToolGuardrailFunctionOutput.reject_content(
            "Rejected: a visit count needs a patient count beside it, so small groups can be checked. "
            "Add count(DISTINCT PATIENT) AS patients."
        )
    for i, column in enumerate(result.get("columns", [])):
        if not COUNT_COLUMN.search(column.lower()):
            continue
        small = [row[i] for row in result["rows"] if isinstance(row[i], int) and 0 < row[i] < SMALLEST_GROUP]
        if small:
            return ToolGuardrailFunctionOutput.reject_content(
                f"Rejected: column '{column}' has groups of fewer than {SMALLEST_GROUP} ({small[:5]}). "
                f"Leave them out with HAVING count(DISTINCT PATIENT) >= {SMALLEST_GROUP}, "
                f"and say 'fewer than {SMALLEST_GROUP}' if they matter."
            )
    return ToolGuardrailFunctionOutput.allow()


# --- On the answer ---


@output_guardrail
async def no_identifiers_in_answer(ctx, agent, output) -> GuardrailFunctionOutput:
    """Stop the run if the answer the user would see contains a patient identifier."""
    found = find_identifiers(str(output))
    return GuardrailFunctionOutput(output_info={"found": found}, tripwire_triggered=bool(found))
