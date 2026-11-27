import json
import uuid
from pathlib import Path

import uvicorn
from agents import Agent, ModelSettings, Runner, RunHooks, SQLiteSession, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

load_dotenv()

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes every call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})

# Placeholder: one agent that just replies. The Desk router replaces it later.
agent = Agent(
    name="CareMetrics Watch",
    instructions="You are CareMetrics Watch, an assistant for hospital data analysts. Reply briefly.",
    model=MODEL,
    model_settings=SETTINGS,
)


class RecordToolCalls(RunHooks):
    """Records each tool call for the page to show. Make a new one per request."""

    def __init__(self):
        self.tool_calls = []

    async def on_tool_end(self, context, agent, tool, result):
        # agent.name tells the page which (sub-)agent made the call.
        self.tool_calls += [{
            "name": tool.name,
            "args": json.loads(context.tool_arguments),
            "result": str(result),
            "agent": agent.name,
        }]


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
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    # A new SQLiteSession on the same file sees the same history.
    session_id = request.session_id or str(uuid.uuid4())
    session = SQLiteSession(session_id, SESSIONS_DB)
    hooks = RecordToolCalls()

    try:
        result = await Runner.run(agent, request.message, session=session, hooks=hooks)
        response, tool_calls = result.final_output, hooks.tool_calls
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response, session_id=session_id, tool_calls=tool_calls)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
