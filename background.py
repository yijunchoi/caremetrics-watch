"""Background agents: sub-agents that work while the agent that started them keeps going.

Copied from the wikipedia-deck-researchers starter (Claude Code's pattern). The Lead will start
one Breakdown Analyst per flagged group, and hear back from each through its inbox.
"""

import asyncio

from agents import Agent, Runner, RunHooks
from pydantic import BaseModel


def notification(agent_id: str, description: str, run: asyncio.Task) -> str:
    """The message the main agent gets when a background agent stops, in Claude Code's format."""
    if run.cancelled():
        status, summary, result = "stopped", f'Agent "{description}" was stopped', ""
    elif run.exception():
        status, summary, result = "failed", f'Agent "{description}" failed: {run.exception()}', ""
    else:
        status, summary, result = "completed", f'Agent "{description}" completed', run.result().final_output
        if isinstance(result, BaseModel):  # a structured answer: send it as JSON
            result = result.model_dump_json()
    return (
        f"<task-notification>\n<task-id>{agent_id}</task-id>\n<status>{status}</status>\n"
        f"<summary>{summary}</summary>\n<result>{result}</result>\n</task-notification>"
    )


class BackgroundAgents:
    """Sub-agents that work while the agent that started them keeps going, as in Claude Code.

    Pass this object as the run's context, and call start() from an async tool: the SDK runs a sync
    tool in a worker thread, which has no event loop to start a run on. start() returns at once.
    run() is an inbox loop: the main agent takes a turn with whatever is in its inbox, its turn can
    end while sub-agents work, and each sub-agent that stops puts a <task-notification> message in
    the inbox, which wakes it for another turn.
    """

    def __init__(self, hooks: RunHooks):
        self.hooks = hooks
        self.runs: list[asyncio.Task] = []
        self.inbox: asyncio.Queue[str] = asyncio.Queue()  # messages for the main agent: the user's, and each sub-agent's notification
        self.unread = 0  # messages the main agent hasn't read, counting sub-agents that haven't stopped

    def start(self, agent: Agent, prompt: str, description: str) -> str:
        """Start agent on prompt in the background. Returns the message for the tool to return."""
        agent_id = f"agent-{len(self.runs) + 1}"
        # A numbered copy, so the page can tell the runs apart
        agent = agent.clone(name=f"{agent.name} {len(self.runs) + 1}")
        # create_task, not await: the run starts now, and start() returns without waiting for it
        run = asyncio.create_task(Runner.run(agent, prompt, context=self, hooks=self.hooks, max_turns=10))
        # When the run stops, however it stops, queue its notification
        run.add_done_callback(lambda run: self.inbox.put_nowait(notification(agent_id, description, run)))
        self.runs.append(run)
        self.unread += 1
        return (
            f"Async agent launched successfully.\nagent_id: {agent_id}\nThe agent is working in the background. "
            "You will be notified automatically when it completes. Don't wait for it or check on it."
        )

    async def run(self, agent: Agent, input: str, max_turns: int = 20):
        """Run agent until its turn ends with no messages left to read. Returns the last turn's result."""
        self.inbox.put_nowait(input)  # the user's message is the first in the inbox
        self.unread += 1
        history = []
        while True:
            # Wait for a message, plus any others that arrived during the last turn
            messages = [await self.inbox.get()]
            while not self.inbox.empty():
                messages.append(self.inbox.get_nowait())
            self.unread -= len(messages)
            history += [{"role": "user", "content": message} for message in messages]
            result = await Runner.run(agent, history, context=self, hooks=self.hooks, max_turns=max_turns)
            history = result.to_input_list()
            if not self.unread:
                return result  # its turn is over, and no sub-agent is still out

    def cancel(self) -> None:
        for run in self.runs:
            run.cancel()
