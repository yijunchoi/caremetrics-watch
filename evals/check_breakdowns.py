"""Check the Breakdown Analysts against the causes in evals/planted.json.

Starts one Breakdown Analyst in the background per flagged group, k times each, and waits for
all of them. A run is right when:
1. it reports every segment of the planted cause (none, when the key says null), and
2. any other segment it reports really explains the change in the data (detect.explaining_segments).
   Plants have side effects: the Humana drop was all 18-34 visits, so Humana also explains much of
   the 18-34 drop. Those segments are right answers the key doesn't list; they print as "extra".
A group passes only if all k runs are right: pass^k.

    uv run python -m evals.check_breakdowns        # k = 3
    uv run python -m evals.check_breakdowns 1      # one run per group, quicker
"""

import asyncio
import json
import sys
from pathlib import Path

from agents import RunHooks

from background import BackgroundAgents
from detect import break_down, detect, explaining_segments
from team import breakdown
from tools.db import db

ROOT = Path(__file__).parent.parent
K = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def text(segments: set) -> str:
    return " + ".join(f"{d}={s}" for d, s in sorted(segments)) or "none"


async def main():
    key = json.loads((ROOT / "evals" / "planted.json").read_text())
    causes = {(c["dimension"], c["group"]): c["cause"] or {} for c in key["changes"]}
    flagged = [(r["dimension"], r["group"]) for r in detect(db.cursor()) if r["flagged"]]

    # Start every run in the background at once, then wait for them all.
    background = BackgroundAgents(RunHooks())
    jobs = []
    for dimension, group in flagged:
        for _ in range(K):
            breakdown.start(background, dimension, group)
            jobs.append((dimension, group))
    print(f"Started {len(jobs)} Breakdown Analysts ({len(flagged)} flagged groups x {K})...")
    results = await asyncio.gather(*background.runs, return_exceptions=True)

    passed = 0
    for dimension, group in flagged:
        planted = set(causes.get((dimension, group), {}).items())  # an unplanted group has no cause
        in_data = explaining_segments(break_down(db.cursor(), dimension, group))
        allowed = planted | in_data

        lines, ok = [], True
        for job, result in zip(jobs, results):
            if job != (dimension, group):
                continue
            if isinstance(result, Exception):
                ok = False
                lines.append(f"x failed: {type(result).__name__}")
                continue
            got = {(s.dimension, s.segment) for s in result.final_output.explained_by}
            right = planted <= got <= allowed
            ok &= right
            extra = got - planted
            lines.append(f"{'=' if right else 'x'} {text(got)}" + (f"   (extra: {text(extra)})" if extra else ""))
        passed += ok
        print(f"\n{'ok  ' if ok else 'FAIL'} {dimension} / {group}")
        print(f"       planted: {text(planted)}   in the data: {text(in_data)}")
        for line in lines:
            print(f"       {line}")

    print(f"\npass^{K}: {passed} of {len(flagged)} groups right in all {K} runs")
    print("PASS" if passed == len(flagged) else "FAIL")


asyncio.run(main())
