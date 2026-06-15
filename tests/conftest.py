import pytest

from spliti.config import get_settings

TEST_PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def test_settings(monkeypatch):
    """Route settings through env vars; never touch the real .env values."""
    monkeypatch.setenv("BASIC_AUTH_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


AUTH = ("anyuser", TEST_PASSWORD)
