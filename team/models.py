"""The model every agent uses, in one place."""

from agents import ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

# LiteLLM routes every call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})
