"""Find which groups of visits changed in the latest month. Plain SQL, no AI.

For every group (each visit type, insurer, diagnosis and age band), count its visits per month.
Then compare the latest month with the 12 months before it:

    z = (latest count - mean of the 12 months) / standard deviation of the 12 months

z says how many "normal wobbles" away from usual the latest month is. We flag |z| > 2,
so drops count as well as rises.
"""

import duckdb

# Each dimension is one SQL expression over the encounters table.
DIMENSIONS = {
    "visit_type": "ENCOUNTERCLASS",
    "insurer": "(SELECT NAME FROM payers WHERE payers.Id = encounters.PAYER)",
    "diagnosis": "REASONDESCRIPTION",  # the reason for the visit; NULL for most check-ups
    "age_band": "AGE_BAND",  # made in build_data.py from AGE_AT_VISIT
}

THRESHOLD = 2.0  # flag when |z| is above this
# Skip groups that average fewer visits a month. With ~30 visits a month, 15 extra visits
# already give z = 3, and 12 months are too few to know the normal wobble of a group that small.
MIN_MONTHLY = 50
BASELINE_MONTHS = 12


def monthly_z_scores(db, dimension: str) -> list[dict]:
    """One row per group in this dimension: latest month vs the 12 months before it."""
    rows = db.execute(f"""
        WITH visits AS (
            SELECT date_trunc('month', START) AS month, {DIMENSIONS[dimension]} AS grp
            FROM encounters
        ),
        -- Every (group, month) pair, so a month with no visits counts as 0, not as missing.
        grid AS (
            SELECT g.grp, m.month
            FROM (SELECT DISTINCT grp FROM visits WHERE grp IS NOT NULL) g
            CROSS JOIN (SELECT DISTINCT month FROM visits) m
        ),
        counts AS (
            SELECT grid.grp, grid.month, count(visits.month) AS n
            FROM grid LEFT JOIN visits USING (grp, month)
            GROUP BY ALL
        ),
        latest AS (SELECT max(month) AS month FROM visits)
        SELECT c.grp,
               max(c.n) FILTER (WHERE c.month = l.month) AS latest,
               avg(c.n) FILTER (WHERE c.month < l.month) AS mean,
               stddev_samp(c.n) FILTER (WHERE c.month < l.month) AS sd
        FROM counts c, latest l
        WHERE c.month >= l.month - INTERVAL {BASELINE_MONTHS} MONTH
        GROUP BY c.grp
    """).fetchall()

    results = []
    for group, latest, mean, sd in rows:
        if mean < MIN_MONTHLY or not sd:
            continue
        z = (latest - mean) / sd
        results.append({
            "dimension": dimension,
            "group": group,
            "latest": latest,
            "mean": round(mean, 1),
            "sd": round(sd, 1),
            "z": round(z, 2),
            "change_pct": round((latest - mean) / mean * 100, 1),
            "flagged": abs(z) > THRESHOLD,
        })
    return results


def detect(db) -> list[dict]:
    """All groups in all dimensions, biggest |z| first."""
    rows = [row for dimension in DIMENSIONS for row in monthly_z_scores(db, dimension)]
    return sorted(rows, key=lambda row: -abs(row["z"]))


if __name__ == "__main__":
    db = duckdb.connect("data/hospital.duckdb", read_only=True)
    rows = detect(db)
    print(f"{'dimension':<11} {'group':<45} {'latest':>7} {'mean':>8} {'sd':>6} {'z':>7} {'change':>8}")
    for row in rows:
        mark = "  <- flagged" if row["flagged"] else ""
        print(f"{row['dimension']:<11} {row['group'][:45]:<45} {row['latest']:>7} {row['mean']:>8} "
              f"{row['sd']:>6} {row['z']:>7} {row['change_pct']:>+7}%{mark}")
    print(f"\n{sum(r['flagged'] for r in rows)} of {len(rows)} groups flagged (|z| > {THRESHOLD})")
