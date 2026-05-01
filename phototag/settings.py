import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Device = Literal["auto", "cpu", "cuda"]


def _default_models_dir() -> Path:
    """Per-user model cache, independent of any single library bundle.

    Models (RAM++ ~5 GB, InsightFace ~200 MB, CLIP ~600 MB) don't change
    per library and shouldn't be duplicated alongside `db_path` — that
    would balloon every backup / rsync of the library. XDG cache is the
    standard Linux/macOS convention; `APP_MODELS_DIR` overrides for
    users who want it elsewhere (NAS, faster SSD, shared network mount).
    """
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "phototag" / "models"


class Settings(BaseSettings):
    log_level: str = "INFO"
    json_logs: bool | None = None
    # `db_path.parent` is the **library bundle anchor**: the DB itself,
    # the `pictures/` symlink (#26), the thumbs/previews/face-thumbs
    # caches, and the backups/ directory all live under it. Move
    # `data/` somewhere else and everything follows automatically.
    db_path: Path = Path("phototag.db")
    # Models, by contrast, are per-user and shared across libraries —
    # default lives outside the library bundle (XDG cache).
    models_dir: Path = _default_models_dir()
    device: Device = "auto"
    # When set, every API call must carry `X-API-Token: <value>` (or
    # `?token=<value>` query string for the GET asset URLs the browser
    # loads natively). Optional — empty/None disables auth entirely
    # (the local-loopback default).
    api_token: str | None = None
    # When set, the middleware re-reads this file on every protected
    # request (whitespace-stripped contents = the active token).
    # Editing the file rotates the token without restart. Takes
    # precedence over `api_token` when both are configured.
    api_token_file: Path | None = None

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    @property
    def data_dir(self) -> Path:
        """Single anchor for derived per-library state (caches + backups).
        Equals `db_path.parent` — moving the DB moves the whole bundle.
        Models are explicitly NOT under this anchor (see `models_dir`)."""
        return self.db_path.parent


def load() -> Settings:
    return Settings()
