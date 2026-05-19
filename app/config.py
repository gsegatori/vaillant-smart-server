from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_file() -> str:
    """Cerca il .env in piu' posti (env var override / CWD / repo root via __file__)."""
    override = os.environ.get("VAILLANT_ENV_FILE")
    if override and Path(override).is_file():
        return override
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return str(cwd_env)
    repo_env = Path(__file__).resolve().parent.parent / ".env"
    if repo_env.is_file():
        return str(repo_env)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_resolve_env_file(), extra="ignore", case_sensitive=False)

    bind_host: str = "0.0.0.0"
    bind_port: int = 5000

    vaillant_user: str = ""
    vaillant_password: str = ""
    vaillant_brand: str = "vaillant"
    vaillant_country: str = "italy"

    cache_file: Path = Path("/data/cache.json")

    cache_ttl_system_info: int = 300
    cache_ttl_zone_info: int = 300
    cache_ttl_zone_flow: int = 300
    cache_ttl_zones: int = 1800
    cache_ttl_water_pressure: int = 600
    cache_ttl_gas: int = 14400

    retries: int = 3
    retry_backoff_base_s: float = 2.0

    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
