import json
import uuid
from pathlib import Path

import uvicorn
from agents import OutputGuardrailTripwireTriggered, Runner, RunHooks, SQLiteSession, set_tracing_disabled
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from team.desk import desk
from tools.entities import expand, find

load_dotenv()

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)


class RecordToolCalls(RunHooks):
    """Records each tool call and handoff for the page to show. Make a new one per request."""

    def __init__(self):
        self.tool_calls = []

    async def on_tool_end(self, context, agent, tool, result):
        # agent.name tells the page which agent made the call.
        self.tool_calls += [{
            "name": tool.name,
            "args": json.loads(context.tool_arguments),
            "result": str(result),
            "agent": agent.name,
        }]

    async def on_handoff(self, context, from_agent, to_agent):
        self.tool_calls += [{"name": "handoff", "args": {"to": to_agent.name}, "result": "", "agent": from_agent.name}]


SESSIONS_DB = "conversations.db"

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(Path(__file__).parent / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # A new SQLiteSession on the same file sees the same history.
    session_id = request.session_id or str(uuid.uuid4())
    session = SQLiteSession(session_id, SESSIONS_DB)
    hooks = RecordToolCalls()

    # Named entity matching runs before the agents, like retrieval in RAG. Record it like a tool call.
    matches = find(request.message)
    if matches:
        hooks.tool_calls += [{"name": "match_entities", "args": {"question": request.message},
                              "result": "\n".join(matches), "agent": "app"}]

    try:
        # Every message starts at the Desk; the session gives it the conversation so far.
        result = await Runner.run(desk, expand(request.message), session=session, hooks=hooks, max_turns=20)
        response, tool_calls = str(result.final_output), hooks.tool_calls
    except OutputGuardrailTripwireTriggered as e:
        # The answer had a patient identifier in it: show why, never the answer itself.
        found = ", ".join(e.guardrail_result.output.output_info["found"])
        response = f"I can't share that: the answer included patient-identifying details ({found})."
        tool_calls = hooks.tool_calls
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", hooks.tool_calls

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await SQLiteSession(session_id, SESSIONS_DB).clear_session()
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
