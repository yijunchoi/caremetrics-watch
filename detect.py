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


def break_down(db, dimension: str, group: str, top: int = 5) -> dict:
    """Split one group's change by each other dimension: which segments the change came from.

    For each segment: its usual monthly visits (mean of the 12 months before the latest), its
    latest count, its change, its share of the group's change, and its usual share of the group's
    visits. A segment whose share of the change is far above its usual share is where it happened.
    """
    latest_month = db.execute("SELECT date_trunc('month', max(START)) FROM encounters").fetchone()[0]
    params = {"group": group, "latest": latest_month}
    in_window = f"START >= $latest - INTERVAL {BASELINE_MONTHS} MONTH"

    latest, usual = db.execute(f"""
        SELECT count(*) FILTER (WHERE START >= $latest),
               count(*) FILTER (WHERE START < $latest) / {BASELINE_MONTHS}
        FROM encounters WHERE {DIMENSIONS[dimension]} = $group AND {in_window}
    """, params).fetchone()
    change = latest - usual

    breakdowns = {}
    for other in DIMENSIONS:
        if other == dimension:
            continue
        rows = db.execute(f"""
            SELECT coalesce({DIMENSIONS[other]}, '(none recorded)') AS segment,
                   count(*) FILTER (WHERE START >= $latest) AS latest,
                   count(*) FILTER (WHERE START < $latest) / {BASELINE_MONTHS} AS usual
            FROM encounters WHERE {DIMENSIONS[dimension]} = $group AND {in_window}
            GROUP BY 1
        """, params).fetchall()
        segments = []
        for segment, seg_latest, seg_usual in rows:
            share_of_change = (seg_latest - seg_usual) / change * 100
            usual_share = seg_usual / usual * 100
            segments.append({
                "segment": segment,
                "usual": round(seg_usual, 1),
                "latest": seg_latest,
                "change": round(seg_latest - seg_usual, 1),
                "share_of_change_pct": round(share_of_change),
                "usual_share_pct": round(usual_share),
                # If the change were spread evenly, share_of_change would equal usual_share.
                "points_above_usual": round(share_of_change - usual_share),
            })
        # The segments that moved most, either way.
        breakdowns[other] = sorted(segments, key=lambda s: -abs(s["change"]))[:top]

    return {"dimension": dimension, "group": group, "latest": latest, "usual": round(usual, 1),
            "change": round(change, 1), "breakdowns": breakdowns}


# A segment explains a change when it holds at least half of the change, and that is well
# above its usual share of the group's visits. The Breakdown Analyst's instructions say the same.
MIN_SHARE_OF_CHANGE = 50
MIN_POINTS_ABOVE_USUAL = 30


def explaining_segments(breakdown: dict) -> set[tuple[str, str]]:
    """The (dimension, segment) pairs in a break_down() result that explain the change."""
    return {
        (dimension, s["segment"])
        for dimension, segments in breakdown["breakdowns"].items()
        for s in segments
        if s["share_of_change_pct"] >= MIN_SHARE_OF_CHANGE
        and s["points_above_usual"] >= MIN_POINTS_ABOVE_USUAL
        and s["segment"] != "(none recorded)"
    }


if __name__ == "__main__":
    db = duckdb.connect("data/hospital.duckdb", read_only=True)
    rows = detect(db)
    print(f"{'dimension':<11} {'group':<45} {'latest':>7} {'mean':>8} {'sd':>6} {'z':>7} {'change':>8}")
    for row in rows:
        mark = "  <- flagged" if row["flagged"] else ""
        print(f"{row['dimension']:<11} {row['group'][:45]:<45} {row['latest']:>7} {row['mean']:>8} "
              f"{row['sd']:>6} {row['z']:>7} {row['change_pct']:>+7}%{mark}")
    print(f"\n{sum(r['flagged'] for r in rows)} of {len(rows)} groups flagged (|z| > {THRESHOLD})")
