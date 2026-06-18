"""Settings for the standalone Spliti app, loaded from env / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # HTTP Basic Auth for the UI and API (any username; this password).
    basic_auth_password: str = ""

    # Mistral key powers the optional Q&A chat and expense-description suggestions.
    # Leave empty to run without any AI features.
    mistral_api_key: str = ""

    # Web Push (VAPID). Leave the keys empty to run with notifications disabled —
    # the API then reports the feature as unavailable and the UI hides it, the
    # same way chat degrades without a Mistral key. Generate a pair with
    # `python scripts/gen_vapid.py`. The private key is a secret; the public
    # (application server) key is handed to the browser to subscribe.
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
