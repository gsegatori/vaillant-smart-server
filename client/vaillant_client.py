import json
import os
import logging
import asyncio
from datetime import datetime, timedelta, UTC
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo

from myPyllant.api import MyPyllantAPI
from myPyllant.const import DEFAULT_BRAND
from myPyllant.enums import DeviceDataBucketResolution, ZoneOperatingMode
from dotenv import load_dotenv

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s - %(levelname)s - %(message)s")

USER = os.getenv("VAILLANT_USER")
PASSWORD = os.getenv("VAILLANT_PASSWORD")
BRAND = os.getenv("VAILLANT_BRAND", DEFAULT_BRAND)
COUNTRY = os.getenv("VAILLANT_COUNTRY")

api: Optional[MyPyllantAPI] = None

CACHE: Dict[str, Any] = {}
CACHE_TTL: Dict[str, datetime] = {}

CACHE_TIMES = {
    "system_info": timedelta(minutes=5),
    "zone_info": timedelta(minutes=30),
    "gas_consumption": timedelta(hours=4),
    "water_pressure": timedelta(minutes=5),
    "zones": timedelta(minutes=5),
    "zone_flow_temp": timedelta(minutes=5)
}


def set_cache(key: str, value: Any, ttl: timedelta):
    CACHE[key] = value
    CACHE_TTL[key] = datetime.now(UTC) + ttl


def get_from_cache(key: str):
    if key in CACHE and CACHE_TTL[key] > datetime.now(UTC):
        return CACHE[key]
    return None


async def init_api():
    global api
    if api is None:
        logging.info("Initializing API...")
        api = MyPyllantAPI(USER, PASSWORD, BRAND, COUNTRY)
        await api.login()
        logging.info("API initialized.")
    return api


async def ensure_authenticated():
    if api is None:
        await init_api()

    if api.oauth_session_expires <= datetime.now(UTC):
        logging.info("Token expired. Refreshing...")
        await api.refresh_token()
        logging.info("Token refreshed.")


async def get_gas_consumption(month, year):
    cache_key = f"gas_consumption_{year}_{month}"
    cached = get_from_cache(cache_key)
    if cached:
        return cached

    logging.info(f"Fetching gas consumption for {year}-{month}")
    await ensure_authenticated()
    async for system in api.get_systems():
        for device in system.devices:
            if device.device_type == "BOILER":
                start_date = datetime(year, month, 1)
                end_date = datetime(year + 1, 1, 1) - timedelta(seconds=1) if month == 12 else datetime(year, month + 1,
                                                                                                        1) - timedelta(
                    seconds=1)
                async for data in api.get_data_by_device(device, DeviceDataBucketResolution.MONTH, start_date,
                                                         end_date):
                    if data.operation_mode == "DOMESTIC_HOT_WATER" and data.energy_type == "CONSUMED_PRIMARY_ENERGY":
                        for bucket in data.data:
                            value_m3 = bucket.value / 10000
                            result = {"consumption_m3": value_m3}
                            set_cache(cache_key, result, CACHE_TIMES["gas_consumption"])
                            return result
    return {"error": "No data found"}


async def get_water_pressure():
    cache_key = "water_pressure"
    cached = get_from_cache(cache_key)
    if cached:
        return cached

    await ensure_authenticated()
    async for system in api.get_systems():
        result = {"pressure": system.water_pressure}
        set_cache(cache_key, result, CACHE_TIMES["water_pressure"])
        return result
    return {"error": "No data found"}


async def get_zones():
    cache_key = "zones"
    cached = get_from_cache(cache_key)
    if cached:
        return cached

    await ensure_authenticated()
    async for system in api.get_systems():
        zones_info = [{"index": i, "name": zone.name} for i, zone in enumerate(system.zones)]
        result = {"zones": zones_info}
        set_cache(cache_key, result, CACHE_TIMES["zones"])
        return result
    return {"error": "No data found"}


async def get_zone_info(zone_index):
    cache_key = f"zone_info_{zone_index}"
    cached = get_from_cache(cache_key)
    if cached:
        return cached

    await ensure_authenticated()
    async for system in api.get_systems():
        if 0 <= zone_index < len(system.zones):
            zone = system.zones[zone_index]
            result = {
                "index": zone_index,
                "name": zone.name,
                "current_temperature": zone.current_room_temperature,
                "desired_temperature": zone.desired_room_temperature_setpoint,
                "heating_state": zone.heating.operation_mode_heating
            }
            set_cache(cache_key, result, CACHE_TIMES["zone_info"])
            return result
    return {"error": "Zone not found"}


async def get_zone_flow_temperature(index):
    cache_key = f"zone_flow_temp_{index}"
    cached = get_from_cache(cache_key)
    if cached:
        return cached

    await ensure_authenticated()
    async for system in api.get_systems():
        if 0 <= index < len(system.zones):
            zone = system.zones[index]
            flow_temp = zone.associated_circuit.current_circuit_flow_temperature
            if flow_temp is not None:
                result = {"flow_temperature": flow_temp}
                set_cache(cache_key, result, CACHE_TIMES["zone_flow_temp"])
                return result
            return {"error": "Flow temperature not available for this zone"}
    return {"error": "Zone not found"}


async def update_zone_mode(zone_index, mode):
    logging.debug(f"Updating zone {zone_index} mode to {mode}")
    await ensure_authenticated()

    async for system in api.get_systems():
        if 0 <= zone_index < len(system.zones):
            zone = system.zones[zone_index]
            mode_map = {
                "manual": ZoneOperatingMode.MANUAL,
                "off": ZoneOperatingMode.OFF,
                "time_controlled": ZoneOperatingMode.TIME_CONTROLLED
            }
            new_mode = mode_map.get(mode.lower())

            if new_mode is None:
                logging.error(f"Invalid mode: {mode}")
                return {"error": "Invalid mode"}

            try:
                url = f"{await api.get_system_api_base(zone.system_id)}/zones/{zone.index}/heating-operation-mode"
                await api.aiohttp_session.patch(
                    url,
                    json={"operationMode": new_mode.name},
                    headers=api.get_authorized_headers(),
                )
                logging.debug(f"Zone {zone.name} mode updated to {mode}")
                return {"message": f"Zone {zone.name} mode set to {mode}"}
            except Exception as e:
                logging.error(f"Failed to update mode for zone {zone.name}: {e}")
                return {"error": f"Failed to update mode for zone {zone.name}: {e}"}

    logging.error(f"Zone {zone_index} not found")
    return {"error": "Zone not found"}


async def update_zone_temperature(zone_index, temperature):
    logging.debug(f"Setting temperature for zone {zone_index} to {temperature}°C")
    await ensure_authenticated()

    async for system in api.get_systems():
        if 0 <= zone_index < len(system.zones):
            zone = system.zones[zone_index]
            try:
                await api.set_manual_mode_setpoint(zone, temperature, "heating")
                logging.debug(f"Temperature for zone {zone.name} set to {temperature}°C")
                return {"message": f"Temperature for zone {zone.name} set to {temperature}°C"}
            except Exception as e:
                logging.error(f"Failed to set temperature for zone {zone.name}: {e}")
                return {"error": f"Failed to set temperature for zone {zone.name}: {e}"}

    logging.error(f"Zone {zone_index} not found")
    return {"error": "Zone not found"}


async def get_system_info():
    cache_key = "system_info"
    cached = get_from_cache(cache_key)
    if cached:
        return cached

    await ensure_authenticated()
    async for system in api.get_systems():
        def serialize(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            elif isinstance(obj, ZoneInfo):
                return str(obj)
            elif hasattr(obj, "__dict__"):
                return obj.__dict__
            return str(obj)

        result = json.loads(json.dumps(system, default=serialize, indent=4))
        set_cache(cache_key, result, CACHE_TIMES["system_info"])
        return result

    return {"error": "No system found"}


if __name__ == "__main__":
    async def main():
        await init_api()
        result = await get_zone_flow_temperature(0)
        logging.info(f"Zone flow temperature: {result}")

        result = await get_system_info()
        print(result)

        if api is not None:
            await api.aiohttp_session.close()


    asyncio.run(main())
