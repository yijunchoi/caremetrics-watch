"""Text2SQL: the Analyst writes SQL, and run_sql runs it against data/hospital.duckdb."""

import json
from pathlib import Path

import duckdb
from agents import function_tool

DB_PATH = Path(__file__).parent.parent / "data" / "hospital.duckdb"
MAX_ROWS = 100

try:
    # The security boundary is the connection, not the prompt: read-only, no file or network
    # access, and the agent can't SET its way out of either.
    db = duckdb.connect(
        str(DB_PATH),
        read_only=True,
        config={"enable_external_access": False, "lock_configuration": True},
    )
except duckdb.Error:
    raise SystemExit("No database found. Run `uv run build_data.py` first.")


@function_tool
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
