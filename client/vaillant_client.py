import json
import os
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional
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
    logging.info(f"Fetching gas consumption for {year}-{month}")
    await ensure_authenticated()
    async for system in api.get_systems():
        if system.devices:
            for device in system.devices:
                if device.device_type == "BOILER":
                    start_date = datetime(year, month, 1)
                    end_date = datetime(year + 1, 1, 1) - timedelta(seconds=1) if month == 12 else datetime(year,
                                                                                                            month + 1,
                                                                                                            1) - timedelta(
                        seconds=1)
                    async for data in api.get_data_by_device(device, DeviceDataBucketResolution.MONTH, start_date,
                                                             end_date):
                        if data.operation_mode == "DOMESTIC_HOT_WATER" and data.energy_type == "CONSUMED_PRIMARY_ENERGY":
                            for bucket in data.data:
                                value_m3 = bucket.value / 10000
                                logging.info(f"Gas consumption: {value_m3} m³")
                                return {"consumption_m3": value_m3}
        return {"error": "No Devices found in this system."}


async def get_water_pressure():
    logging.info("Fetching water pressure")
    await ensure_authenticated()
    async for system in api.get_systems():
        return {"pressure": system.water_pressure}
    return {"error": "No pressure found"}


async def get_zones():
    logging.info("Fetching zones")
    await ensure_authenticated()
    async for system in api.get_systems():
        zones_info = [{"index": i, "name": zone.name} for i, zone in enumerate(system.zones)]
        return {"zones": zones_info}
    return {"error": "No zones found"}


async def get_zone_info(zone_index):
    logging.info(f"Fetching zone info for index {zone_index}")
    await ensure_authenticated()
    async for system in api.get_systems():
        if 0 <= zone_index < len(system.zones):
            zone = system.zones[zone_index]
            return {"index": zone_index, "name": zone.name, "current_temperature": zone.current_room_temperature,
                    "desired_temperature": zone.desired_room_temperature_setpoint,
                    "heating_state": zone.heating.operation_mode_heating}
    return {"error": "Zone not found"}


async def get_zone_flow_temperature(index):
    logging.info(f"Fetching flow temperature for zone {index}")
    await ensure_authenticated()
    async for system in api.get_systems():
        if 0 <= index < len(system.zones):
            zone = system.zones[index]
            flow_temp = zone.associated_circuit.current_circuit_flow_temperature
            if flow_temp is not None:
                logging.info(f"Flow temperature for zone {index}: {flow_temp}°C")
                return {"flow_temperature": flow_temp}
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

        return json.loads(json.dumps(system, default=serialize, indent=4))

    return {"error": "No system found"}

if __name__ == "__main__":
    import asyncio

    async def main():
        logging.info("Initializing API...")
        await init_api()
        logging.info("API initialized.")

        now = datetime.now()
        result = await get_gas_consumption(now.month, now.year)
        logging.info(f"Gas consumption: {result}")

        result = await get_zone_flow_temperature(0)
        logging.info(f"Zone flow temperature: {result}")

        result = await get_system_info()
        print(result)


        if api is not None:
            await api.aiohttp_session.close()
            logging.info("HTTP session closed.")


    asyncio.run(main())
