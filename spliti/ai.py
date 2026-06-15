"""Lazily-constructed Mistral client shared by the AI features (ask + suggestions)."""

from mistralai.client import Mistral

from spliti.config import get_settings

MODEL = "mistral-large-latest"

_client: Mistral | None = None


def client() -> Mistral:
    global _client
    if _client is None:
        _client = Mistral(api_key=get_settings().mistral_api_key)
    return _client
