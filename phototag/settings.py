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

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")


def load() -> Settings:
    return Settings()
