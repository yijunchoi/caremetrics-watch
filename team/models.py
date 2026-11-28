"""The model every agent uses, in one place."""

from agents import ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

# LiteLLM routes every call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# The Breakdown Analyst needs a stronger model: with a tool and a structured answer, flash-lite
# kept calling break_down again instead of answering (3 of 6 runs hit the turn limit; flash: 0 of 6).
STRONG_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})
