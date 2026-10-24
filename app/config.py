from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from myPyllant.enums import DHWOperationMode
from myPyllant.models import ZoneTimeProgramDay
from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


@dataclass
class Preset:
    """A named schedule preset combining DHW mode and heating day time plan."""

    name: str
    dhw_mode: DHWOperationMode
    heating_plan: list[ZoneTimeProgramDay]

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_PRESETS: dict[str, Preset] = {
    "AT_HOME": Preset(
        name="At Home",
        dhw_mode=DHWOperationMode.TIME_CONTROLLED,
        heating_plan=[
            # 00:00-03:00
            ZoneTimeProgramDay(index=0, weekday_name="all", start_time=0, end_time=180, setpoint=19.5),
            # 03:00-21:00
            ZoneTimeProgramDay(index=1, weekday_name="all", start_time=180, end_time=1260, setpoint=20.5),
            # 21:00-22:00
            ZoneTimeProgramDay(index=2, weekday_name="all", start_time=1260, end_time=1320, setpoint=19.5),
            # 22:00-23:05
            ZoneTimeProgramDay(index=3, weekday_name="all", start_time=1320, end_time=1385, setpoint=15.0),
            # 23:05-00:00
            ZoneTimeProgramDay(index=4, weekday_name="all", start_time=1385, end_time=1440, setpoint=19.5),
        ],
    ),
    "HOLIDAY": Preset(
        name="Holiday",
        dhw_mode=DHWOperationMode.OFF,
        heating_plan=[
            # 00:00-05:55
            ZoneTimeProgramDay(index=0, weekday_name="all", start_time=0, end_time=355, setpoint=17.0),
            # 05:55-23:05
            ZoneTimeProgramDay(index=1, weekday_name="all", start_time=355, end_time=1385, setpoint=15.0),
            # 23:05-00:00
            ZoneTimeProgramDay(index=2, weekday_name="all", start_time=1385, end_time=1440, setpoint=17.0),
        ],
    ),
    # To be set one day before coming back, to warm the house mostly during cheaper electricity hours overnight.
    "COMING_BACK": Preset(
        name="Coming Back",
        dhw_mode=DHWOperationMode.TIME_CONTROLLED,
        heating_plan=[
            # 00:00-05:55
            ZoneTimeProgramDay(index=0, weekday_name="all", start_time=0, end_time=355, setpoint=20.5),
            # 05:55-23:05
            ZoneTimeProgramDay(index=1, weekday_name="all", start_time=355, end_time=1385, setpoint=17.0),
            # 23:05-00:00
            ZoneTimeProgramDay(index=2, weekday_name="all", start_time=1385, end_time=1440, setpoint=20.5),
        ],
    ),
}


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


def _resolve_yaml_file() -> str | None:
    """Look for the YAML configuration file in multiple locations (env var override / CWD / repo root)."""
    override = os.environ.get("VAILLANT_CONFIG_FILE")
    if override and Path(override).is_file():
        return override
    for name in ("config.yaml", "config.yml"):
        cwd_cfg = Path.cwd() / name
        if cwd_cfg.is_file():
            return str(cwd_cfg)
        repo_cfg = Path(__file__).resolve().parent.parent / name
        if repo_cfg.is_file():
            return str(repo_cfg)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        yaml_file=_resolve_yaml_file(),
        extra="ignore",
        case_sensitive=False,
    )

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
    cache_ttl_rts: int = 600
    cache_ttl_device_data: int = 3600
    cache_ttl_system_info_and_data: int = 300

    retries: int = 3
    retry_backoff_base_s: float = 2.0

    # Cache in-memory del System object Vaillant lato client. Tutti i read
    # endpoint (zone-info, water-pressure, zones, system-info, gas) riusano
    # lo stesso fetch entro questo TTL. Le write invalidano dopo PATCH
    # cosi' il prossimo read riprende fresco. Drastica riduzione chiamate /homes.
    system_cache_ttl_s: float = 60.0

    log_level: str = "INFO"

    presets: dict[str, Preset] = Field(default_factory=lambda: dict(DEFAULT_PRESETS))

    @field_validator("presets", mode="before")
    @classmethod
    def _validate_presets(cls, v: Any) -> Any:
        if v is None:
            raise ValueError("presets cannot be None")
        if not isinstance(v, dict):
            raise ValueError("presets must be a dictionary")
        normalized = {}
        for p_key, p_val in v.items():
            if isinstance(p_val, Preset):
                normalized[p_key] = p_val
                continue
            if isinstance(p_val, dict):
                p_copy = dict(p_val)
                dhw = p_copy.get("dhw_mode")
                if isinstance(dhw, str):
                    p_copy["dhw_mode"] = DHWOperationMode[dhw]
                slots = []
                for i, item in enumerate(p_copy.get("heating_plan", [])):
                    if isinstance(item, ZoneTimeProgramDay):
                        slots.append(item)
                    elif isinstance(item, dict):
                        d = dict(item)
                        d.setdefault("index", i)
                        d.setdefault("weekday_name", "all")
                        slots.append(ZoneTimeProgramDay(**d))
                    else:
                        slots.append(item)
                p_copy["heating_plan"] = slots
                normalized[p_key] = Preset(**p_copy)
            else:
                normalized[p_key] = p_val
        return normalized

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            env_settings,
            DotEnvSettingsSource(settings_cls, env_file=_resolve_env_file()),
        ]
        try:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=_resolve_yaml_file()))
        except Exception:
            pass
        sources.append(file_secret_settings)
        return tuple(sources)


def get_settings() -> Settings:
    return Settings()
