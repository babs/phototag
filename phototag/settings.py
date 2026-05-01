from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Device = Literal["auto", "cpu", "cuda"]


class Settings(BaseSettings):
    log_level: str = "INFO"
    json_logs: bool | None = None
    db_path: Path = Path("phototag.db")
    models_dir: Path = Path("data/models")
    device: Device = "auto"
    # When set, every API call must carry `X-API-Token: <value>` (or
    # `?token=<value>` query string for the GET asset URLs the browser
    # loads natively). Optional — empty/None disables auth entirely
    # (the local-loopback default).
    api_token: str | None = None

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")


def load() -> Settings:
    return Settings()
