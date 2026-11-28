"""The Breakdown Analyst: explains one flagged change. Runs in the background, one per flagged group."""

import json

from agents import Agent, RunContextWrapper, function_tool
from pydantic import BaseModel

from background import BackgroundAgents
from detect import break_down
from team.models import SETTINGS, STRONG_MODEL
from tools.sql import db


@function_tool(name_override="break_down")
def break_down_tool(dimension: str, group: str) -> str:
    """Split one group's Jan 2020 change by each other dimension.

    Args:
        dimension: The flagged group's dimension: 'visit_type', 'insurer', 'diagnosis' or 'age_band'.
        group: The flagged group, exactly as detect.py names it, e.g. 'emergency'.
    """
    # Each call gets its own cursor: DuckDB connections aren't thread-safe.
    with db.cursor() as cursor:
        try:
            return json.dumps(break_down(cursor, dimension, group))
        except (KeyError, ZeroDivisionError) as e:
            return json.dumps({"error": f"Can't break down {dimension} / {group}: {e!r}"})


class Segment(BaseModel):
    dimension: str  # 'visit_type', 'insurer', 'diagnosis' or 'age_band'
    segment: str  # exactly as break_down names it


class Finding(BaseModel):
    dimension: str
    group: str
    explained_by: list[Segment]  # empty: no single segment explains the change
    summary: str  # one or two sentences, with the numbers


breakdown_analyst = Agent(
    name="Breakdown Analyst",
    instructions="""\
You explain one flagged change in a hospital's Jan 2020 visits. Your task names the group.

1. Call break_down once, with the group's dimension and name.
2. For each segment it lists, share_of_change_pct is how much of the group's change came from that
   segment, and usual_share_pct is how much of the group's visits it usually has. If the change
   were spread evenly, the two would be about equal.
3. A segment explains the change when BOTH hold:
   - share_of_change_pct is 50 or more, and
   - points_above_usual is 30 or more.
   '(none recorded)' is never a cause.
4. List every segment that explains the change, from any dimension. A change can have a cause in
   two dimensions at once, e.g. age band 80+ and insurer Medicare. If no segment qualifies, list
   none: the change is spread across the group.
5. In summary, say in one or two plain sentences where the change came from, with the numbers
   (e.g. "Emergency visits rose by 96 to 377; patients 80+ account for 123 of them, though they
   are usually 12% of emergency visits."). If none qualifies, say the change is spread evenly.""",
    model=STRONG_MODEL,
    model_settings=SETTINGS,
    tools=[break_down_tool],
    output_type=Finding,
)


def start(background: BackgroundAgents, dimension: str, group: str) -> str:
    """Start one Breakdown Analyst in the background. Used by start_breakdown and the evals."""
    prompt = f"Explain the Jan 2020 change in {dimension} = {group!r}."
    return background.start(breakdown_analyst, prompt, f"Break down {dimension} / {group}")


@function_tool
async def start_breakdown(ctx: RunContextWrapper[BackgroundAgents], dimension: str, group: str) -> str:
    """Start a Breakdown Analyst in the background on one flagged group. It returns at once.

    Args:
        dimension: The flagged group's dimension: 'visit_type', 'insurer', 'diagnosis' or 'age_band'.
        group: The flagged group, exactly as detect.py names it, e.g. 'emergency'.
    """
    return start(ctx.context, dimension, group)
