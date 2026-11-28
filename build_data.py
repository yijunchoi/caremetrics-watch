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
4. Plant about 20 changes in the last month (Jan 2020), and write them to evals/planted.json
   as the answer key. A rise copies visits of that group from 2019; a drop deletes some of
   its Jan 2020 visits.
5. Move each condition with the encounter it was recorded at.
"""

import json
from pathlib import Path

import duckdb

from detect import DIMENSIONS

DATA = Path(__file__).parent / "data"
RAW = DATA / "raw"
DB_PATH = DATA / "hospital.duckdb"
KEY_PATH = Path(__file__).parent / "evals" / "planted.json"

WINDOW_START = "2018-02-01"  # the first of the 24 months
MONTHS = 24
CUTOFF = "2020-02-01"  # drop encounters on or after this date
LATEST_MONTH = "2020-01-01"  # the month we plant changes in

CHF = "Chronic congestive heart failure (disorder)"

# (dimension, group, change, cause)
# - change: the share of that group's Jan 2020 visits to add (+) or remove (-). Sizes are
#   picked so each lands far above the |z| > 2 flag (about z = 4 to 10).
# - cause: None means the change is spread evenly over the group's visits. A cause like
#   {"age_band": "80+", "insurer": "Medicare"} means only those visits changed, so that is the
#   "why" the Analysts should find. It is saved in the answer key.
# Every visit belongs to all four dimensions, so adding 80+ visits also adds Medicare visits.
# Plants come in pairs that cancel out (80+ up, 65-79 down), so groups we did not plant stay
# flat. Two causes are told from both sides (inpatient <-> CHF, outpatient <-> uninsured).
# evals/check_detect.py checks all of this.
PLANTS = [
    ("visit_type", "emergency", +0.50, {"age_band": "80+", "insurer": "Medicare"}),
    ("visit_type", "urgentcare", +0.50, None),
    ("visit_type", "inpatient", +0.30, {"diagnosis": CHF}),
    ("visit_type", "outpatient", -0.10, {"insurer": "NO_INSURANCE"}),
    ("insurer", "Medicaid", +0.15, None),
    ("insurer", "Humana", -0.20, {"age_band": "18-34"}),
    ("insurer", "NO_INSURANCE", -0.05, {"visit_type": "outpatient"}),
    ("insurer", "Anthem", +0.20, None),
    ("insurer", "Dual Eligible", +0.30, {"visit_type": "ambulatory"}),
    ("diagnosis", "Acute bronchitis (disorder)", +0.80, {"age_band": "0-17"}),
    ("diagnosis", "Viral sinusitis (disorder)", +0.60, None),
    ("diagnosis", CHF, +0.10, {"visit_type": "inpatient"}),
    ("diagnosis", "Acute viral pharyngitis (disorder)", +0.80, None),
    ("diagnosis", "Acute bacterial sinusitis (disorder)", +0.60, None),
    ("diagnosis", "Normal pregnancy", -0.40, None),
    ("diagnosis", "Anemia (disorder)", -0.40, None),
    ("age_band", "0-17", +0.20, None),
    ("age_band", "18-34", -0.10, None),
    ("age_band", "80+", +0.10, None),
    ("age_band", "65-79", -0.20, None),
]

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
             REPLACE (new_start AS START, new_start + (STOP - START) AS STOP),
           -- One shared definition of the age bands, for detect.py and the Analysts' SQL.
           CASE WHEN AGE_AT_VISIT < 18 THEN '0-17'
                WHEN AGE_AT_VISIT < 35 THEN '18-34'
                WHEN AGE_AT_VISIT < 50 THEN '35-49'
                WHEN AGE_AT_VISIT < 65 THEN '50-64'
                WHEN AGE_AT_VISIT < 80 THEN '65-79'
                ELSE '80+' END AS AGE_BAND
    FROM moved
""")

# --- 3. Plant the changes in Jan 2020 ---
in_latest = f"START >= DATE '{LATEST_MONTH}'"
in_2019 = f"START >= DATE '{LATEST_MONTH}' - INTERVAL 12 MONTH AND START < DATE '{LATEST_MONTH}'"
answer_key = []

for i, (dimension, group, change, cause) in enumerate(PLANTS):
    in_group = f"{DIMENSIONS[dimension]} = $group"
    count_sql = f"SELECT count(*) FROM encounters WHERE {in_group} AND {in_latest}"
    before = db.execute(count_sql, {"group": group}).fetchone()[0]
    n = round(before * abs(change))
    # Each plant has its own seed, so it picks its own visits (another die roll on the Id).
    params = {"group": group, "seed": f"plant{i}", "n": n}

    # With a cause, only visits that also match the cause are copied or deleted.
    # e.g. emergency + cause 80+ on Medicare: "... AND AGE_BAND = $c0 AND <insurer> = $c1"
    for j, (cause_dimension, cause_group) in enumerate((cause or {}).items()):
        in_group += f" AND {DIMENSIONS[cause_dimension]} = $c{j}"
        params[f"c{j}"] = cause_group

    if change > 0:
        # Copy n matching 2019 visits into a random day of Jan 2020, as new visits
        # (new Id; same patient, insurer, reason and time of day).
        db.execute(f"""
            INSERT INTO encounters
            SELECT * EXCLUDE (new_start)
                     REPLACE (md5(Id || $seed) AS Id,
                              new_start AS START,
                              new_start + (STOP - START) AS STOP)
            FROM (
                SELECT *,
                       DATE '{LATEST_MONTH}' + to_days((hash(Id || $seed) % 31)::INTEGER)
                         + (START - date_trunc('day', START)) AS new_start
                FROM encounters
                WHERE {in_group} AND {in_2019}
                ORDER BY hash(Id || $seed)
                LIMIT $n
            )
        """, params)
    else:
        # Delete n matching Jan 2020 visits.
        db.execute(f"""
            DELETE FROM encounters WHERE Id IN (
                SELECT Id FROM encounters
                WHERE {in_group} AND {in_latest}
                ORDER BY hash(Id || $seed)
                LIMIT $n
            )
        """, params)

    answer_key.append({
        "dimension": dimension,
        "group": group,
        "direction": "up" if change > 0 else "down",
        "planted_change_pct": round(change * 100),
        "cause": cause,  # null: spread evenly, no single cause to find
        "visits_before": before,  # Jan 2020 visits just before this plant ran
    })

# Later plants overlap earlier ones (a deleted 18-34 visit may be a Medicaid visit),
# so read the final counts once all plants are in.
for row in answer_key:
    in_group = f"{DIMENSIONS[row['dimension']]} = $group"
    sql = f"SELECT count(*) FROM encounters WHERE {in_group} AND {in_latest}"
    row["visits_after"] = db.execute(sql, {"group": row["group"]}).fetchone()[0]

KEY_PATH.parent.mkdir(exist_ok=True)
KEY_PATH.write_text(json.dumps({"month": LATEST_MONTH[:7], "changes": answer_key}, indent=2) + "\n")
print(f"planted {len(answer_key)} changes in {LATEST_MONTH[:7]}; answer key in {KEY_PATH.name}")

# --- 4. Conditions: follow their encounter to its new date ---
# (Built after planting: conditions of deleted visits drop out; copied visits get none.)
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

# --- 5. Check what we built ---
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
