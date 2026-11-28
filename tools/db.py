"""The one connection to data/hospital.duckdb that the app's tools share."""

from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent.parent / "data" / "hospital.duckdb"

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
