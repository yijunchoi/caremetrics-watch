"""Check detect.py and the answer key in evals/planted.json. No AI, so it should pass every time.

Passes when:
1. every planted change is flagged in the right direction with |z| > 3,
2. no group we did not plant gets past |z| > 3 (a chance flag between 2 and 3 is allowed),
3. every recorded cause explains at least half of its group's change, so an Analyst who
   breaks the change down can really find it.
"""

import json
from pathlib import Path

import duckdb

from detect import DIMENSIONS, detect

ROOT = Path(__file__).parent.parent
MIN_CAUSE_SHARE = 0.5

key = json.loads((ROOT / "evals" / "planted.json").read_text())
planted = {(c["dimension"], c["group"]): c for c in key["changes"]}
latest_month = f"DATE '{key['month']}-01'"

db = duckdb.connect(str(ROOT / "data" / "hospital.duckdb"), read_only=True)
rows = {(r["dimension"], r["group"]): r for r in detect(db)}


def change_in(filters: dict) -> float:
    """Jan visits minus the 12-month average, for visits matching every {dimension: group}."""
    where = " AND ".join(f"{DIMENSIONS[dimension]} = ?" for dimension in filters)
    latest, mean = db.execute(f"""
        SELECT count(*) FILTER (WHERE START >= {latest_month}),
               count(*) FILTER (WHERE START >= {latest_month} - INTERVAL 12 MONTH
                                  AND START < {latest_month}) / 12
        FROM encounters WHERE {where}
    """, list(filters.values())).fetchone()
    return latest - mean


failures = []
print("Planted changes:")
for (dimension, group), change in planted.items():
    row = rows.get((dimension, group))
    z = row["z"] if row else None
    ok = z is not None and abs(z) > 3 and (z > 0) == (change["direction"] == "up")
    failures += [] if ok else [f"missed: {dimension} / {group}"]

    cause_note = ""
    if change["cause"]:
        # The cause's share of the change: e.g. extra emergency visits by 80+ Medicare patients,
        # out of all extra emergency visits.
        share = change_in({dimension: group, **change["cause"]}) / change_in({dimension: group})
        cause_ok = share >= MIN_CAUSE_SHARE
        failures += [] if cause_ok else [f"weak cause: {dimension} / {group} ({share:.0%})"]
        cause_note = f"  cause {change['cause']} explains {share:.0%}" + ("" if cause_ok else " WEAK")
    print(f"  {'ok  ' if ok else 'MISS'} {dimension:<11} {group[:40]:<40} {change['direction']:<5} "
          f"z = {z:>6}{cause_note}")

print("\nOther groups past |z| > 2 (chance):")
for key_, row in rows.items():
    if key_ not in planted and row["flagged"]:
        too_big = abs(row["z"]) > 3
        failures += [f"unplanted big move: {key_}"] if too_big else []
        print(f"  {'BIG ' if too_big else '    '} {row['dimension']:<11} {row['group']:<40} z = {row['z']}")

print("\nPASS" if not failures else "\nFAIL\n  " + "\n  ".join(failures))
