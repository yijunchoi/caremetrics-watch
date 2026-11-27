"""Build data/hospital.duckdb from the Synthea CSVs. Run once, offline, before app.py.

The CSVs come from MITRE's prebuilt 10k-patient Synthea sample (10k_synthea_covid19_csv.zip),
unzipped into data/raw/. data/raw/ is in .gitignore; only the .duckdb file is committed.

The raw timeline is uneven (a 4x jump in mid-2019, a COVID spike in Feb-Mar 2020), and that would
hide the changes we plant later. So we reshape it:
1. Cut every encounter from Feb 2020 on.
2. Move each remaining encounter to a month in an even 24-month window (Feb 2018 - Jan 2020),
   picked by hashing its Id, like rolling a fair 24-sided die. The same Id always lands in the
   same place, so every build gives the same file. Counts still vary a little month to month.
3. Keep each patient's age at the original visit in AGE_AT_VISIT, since the new date no longer
   matches BIRTHDATE.
4. Move each condition with the encounter it was recorded at.
"""

from pathlib import Path

import duckdb

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "hospital.duckdb"

WINDOW_START = "2018-02-01"  # the first of the 24 months
MONTHS = 24
CUTOFF = "2020-02-01"  # drop encounters on or after this date

# Start fresh every time, so running the script twice gives the same file.
DB_PATH.unlink(missing_ok=True)
db = duckdb.connect(str(DB_PATH))
# Synthea timestamps end in Z (UTC). Do the date math in UTC, then store plain timestamps.
db.execute("SET TimeZone = 'UTC'")


def load_csv(name: str) -> str:
    """SQL that reads one raw CSV; read_csv guesses each column's type."""
    return f"SELECT * FROM read_csv('{RAW / f'{name}.csv'}')"


# --- 1. Tables we keep as they are ---
db.execute(f"CREATE TABLE patients AS {load_csv('patients')}")
db.execute(f"CREATE TABLE payers AS {load_csv('payers')}")

# --- 2. Encounters: cut, then spread over 24 months ---
db.execute(f"CREATE TEMP TABLE raw_encounters AS {load_csv('encounters')}")
db.execute(f"""
    CREATE TABLE encounters AS
    WITH kept AS (
        SELECT e.* REPLACE (e.START::TIMESTAMP AS START, e.STOP::TIMESTAMP AS STOP),
               -- Full years between birth and the original visit.
               date_sub('year', p.BIRTHDATE, e.START::DATE) AS AGE_AT_VISIT,
               -- The die roll: hash(Id) % 24 picks the month.
               DATE '{WINDOW_START}' + to_months((hash(e.Id) % {MONTHS})::INTEGER) AS new_month
        FROM raw_encounters e
        JOIN patients p ON p.Id = e.PATIENT
        WHERE e.START < TIMESTAMP '{CUTOFF}'
    ),
    moved AS (
        SELECT *,
               -- A second roll picks the day within that month; the time of day stays the same.
               new_month
                 + to_days((hash(Id || 'day') % day(last_day(new_month)))::INTEGER)
                 + (START - date_trunc('day', START)) AS new_start
        FROM kept
    )
    -- The visit keeps its length: STOP moves by the same amount as START.
    SELECT * EXCLUDE (new_month, new_start)
             REPLACE (new_start AS START, new_start + (STOP - START) AS STOP)
    FROM moved
""")

# --- 3. Conditions: follow their encounter to its new date ---
db.execute(f"CREATE TEMP TABLE raw_conditions AS {load_csv('conditions')}")
db.execute("""
    CREATE TABLE conditions AS
    SELECT c.* REPLACE (
               e.START::DATE AS START,
               -- Shift STOP by the same number of days as START (NULL stays NULL: still ongoing).
               c.STOP + (e.START::DATE - c.START)::INTEGER AS STOP)
    FROM raw_conditions c
    JOIN encounters e ON e.Id = c.ENCOUNTER   -- conditions from cut encounters drop out
""")

# --- 4. Check what we built ---
for table in ["patients", "encounters", "conditions", "payers"]:
    rows = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    print(f"{table:<12} {rows:>10,} rows")

first, last = db.execute("SELECT min(START)::DATE, max(START)::DATE FROM encounters").fetchone()
print(f"\nencounters run from {first} to {last}")

months = db.execute("""
    SELECT strftime(START, '%Y-%m') AS month, count(*) AS n
    FROM encounters GROUP BY 1 ORDER BY 1
""").fetchall()
counts = [n for _, n in months]
mean = sum(counts) / len(counts)
print(f"{len(months)} months, {min(counts):,} to {max(counts):,} encounters a month (mean {mean:,.0f})")
for month, n in months:
    print(f"  {month}  {n:>6,}  {(n - mean) / mean:+.1%}")

db.close()
size_mb = DB_PATH.stat().st_size / 1024 / 1024
print(f"\n{DB_PATH.name} is {size_mb:.1f} MB")
