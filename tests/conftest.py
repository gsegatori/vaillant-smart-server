from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.cache import PersistentCache
from app.config import Settings
from app.main import create_app


class FakeVaillantClient:
    """Stub del VaillantClient: niente network, output deterministico."""

    def __init__(self, *args, **kwargs):
        self.calls: dict[str, int] = {}
        self.fail_on: set[str] = set()
        self.force_quota: tuple[str, str] | None = None  # (replenish_in, message)

    def _bump(self, name: str):
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.fail_on:
            raise RuntimeError(f"forced failure in {name}")
        if self.force_quota is not None:
            from app.client import VaillantQuotaExceeded
            rep, msg = self.force_quota
            raise VaillantQuotaExceeded(rep, msg)

    async def close(self):
        pass

    async def get_water_pressure(self):
        self._bump("get_water_pressure")
        return 1.4

    async def get_zones(self):
        self._bump("get_zones")
        return [
            {"index": 0, "name": "Piano 0"},
            {"index": 1, "name": "Piano 1"},
            {"index": 2, "name": "Piano 2"},
        ]

    async def get_zone_info(self, idx: int):
        self._bump(f"get_zone_info[{idx}]")
        if not (0 <= idx < 3):
            return None
        return {
            "index": idx,
            "name": f"Piano {idx}",
            "current_temperature": 21.0 + idx,
            "desired_temperature": 5.0,
            "heating_state": "OFF",
        }

    async def get_zone_flow_temperature(self, idx: int):
        self._bump(f"get_zone_flow_temperature[{idx}]")
        if not (0 <= idx < 3):
            return None
        return 30.0

    async def get_gas_consumption(self, year: int, month: int):
        self._bump(f"get_gas_consumption[{year},{month}]")
        return {
            "year": year,
            "month": month,
            "by_mode_m3": {"DOMESTIC_HOT_WATER": 19.2, "HEATING": 0.0},
            "total_m3": 19.2,
        }

    async def get_gas_consumption_year(self, year: int):
        self._bump(f"get_gas_consumption_year[{year}]")
        return {
            "year": year,
            "by_month": {
                1: {"by_mode_m3": {"DOMESTIC_HOT_WATER": 25.0, "HEATING": 100.0}, "total_m3": 125.0},
                2: {"by_mode_m3": {"DOMESTIC_HOT_WATER": 22.0, "HEATING": 80.0}, "total_m3": 102.0},
            },
            "by_mode_m3": {"DOMESTIC_HOT_WATER": 47.0, "HEATING": 180.0},
            "total_m3": 227.0,
        }

    async def update_zone_mode(self, idx: int, mode: str):
        self._bump(f"update_zone_mode[{idx},{mode}]")
        return {"index": idx, "mode": mode}

    async def set_zone_setpoint(self, idx: int, temperature: float):
        self._bump(f"set_zone_setpoint[{idx},{temperature}]")
        return {"index": idx, "setpoint": temperature}

    async def get_system_info(self):
        self._bump("get_system_info")
        return {"home": "fake", "zones": 3}


@pytest.fixture
def tmp_cache(tmp_path):
    return PersistentCache(tmp_path / "cache.json")


@pytest.fixture
def fake_client():
    return FakeVaillantClient()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        bind_host="127.0.0.1",
        bind_port=5000,
        vaillant_user="x",
        vaillant_password="x",
        cache_file=tmp_path / "cache.json",
        log_level="WARNING",
    )


@pytest.fixture
def client(settings, fake_client, tmp_cache):
    app = create_app(settings=settings, client=fake_client, cache=tmp_cache)
    with TestClient(app) as c:
        yield c
