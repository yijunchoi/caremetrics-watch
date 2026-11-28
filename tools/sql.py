"""Text2SQL: the Analyst writes SQL, and run_sql runs it against data/hospital.duckdb."""

import json

import duckdb
from agents import function_tool

from guardrails import no_identifiers_in_query, no_identifiers_in_result, no_small_groups, select_only
from tools.db import db

MAX_ROWS = 100


@function_tool(
    # Checked before the query runs, then on its result. A rejection goes back to the model as the
    # tool's answer, with the reason, so it can try again within the rules.
    tool_input_guardrails=[select_only, no_identifiers_in_query],
    tool_output_guardrails=[no_identifiers_in_result, no_small_groups],
)
def run_sql(query: str) -> str:
    """Run a DuckDB SQL query against the hospital database and return the results.

    Args:
        query: A DuckDB SQL query.
    """
    # The SDK runs tools in threads. Each call gets its own cursor: DuckDB connections aren't thread-safe.
    with db.cursor() as cursor:
        try:
            cursor.execute(query)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except duckdb.Error as e:
            # The model cannot see an exception. Return something it can reason about.
            return json.dumps({"error": str(e)})
        columns = [column[0] for column in cursor.description or []]
    return json.dumps({"columns": columns, "rows": rows[:MAX_ROWS], "truncated": len(rows) > MAX_ROWS}, default=str)
