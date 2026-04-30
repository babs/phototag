from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    log_level: str = "INFO"
    json_logs: bool | None = None
    db_path: Path = Path("phototag.db")
    models_dir: Path = Path("data/models")
    device: str = "auto"

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")


def load() -> Settings:
    return Settings()
