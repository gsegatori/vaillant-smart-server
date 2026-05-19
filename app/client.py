"""Wrapper async attorno a myPyllant.

- Login lazy + token refresh automatico.
- Retry exp-backoff su transient errors.
- Login lock per evitare doppi login concorrenti.
- Tutti i metodi ritornano dict serializzabili (no oggetti myPyllant), cosi' la cache JSON funziona.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from myPyllant.api import MyPyllantAPI
from myPyllant.enums import DeviceDataBucketResolution, ZoneOperatingMode

log = logging.getLogger(__name__)

_QUOTA_REPLENISH_RE = re.compile(r"Quota will be replenished in (\d+:\d+:\d+)")


class VaillantQuotaExceeded(Exception):
    """L'API Vaillant ha respinto la richiesta per quota esaurita.

    Carries replenish_in (formato HH:MM:SS) e il messaggio originale dell'API
    per surface up agli endpoint, che lo convertono in HTTP 429 con detail
    strutturato per la UI.
    """

    def __init__(self, replenish_in: str, message: str = "") -> None:
        self.replenish_in = replenish_in
        self.message = message
        super().__init__(f"Vaillant quota exceeded, replenish in {replenish_in}")


class VaillantClient:
    """Client thread-safe-async per myPyllant con retry interno."""

    def __init__(
        self,
        user: str,
        password: str,
        brand: str,
        country: str,
        retries: int = 3,
        backoff_base_s: float = 2.0,
        system_cache_ttl_s: float = 60.0,
    ) -> None:
        self._user = user
        self._password = password
        self._brand = brand
        self._country = country
        self._retries = retries
        self._backoff = backoff_base_s
        self._api: MyPyllantAPI | None = None
        self._login_lock = asyncio.Lock()

        # Cache in-memory del System: TUTTI i read endpoint riusano lo
        # stesso fetch entro questo TTL. Le write invalidano dopo PATCH.
        self._system_cache_ttl: float = system_cache_ttl_s
        self._system_cache: Any | None = None
        self._system_cache_at: float = 0.0
        self._system_lock = asyncio.Lock()  # single-flight su get_systems()

    async def close(self) -> None:
        if self._api is not None and self._api.aiohttp_session is not None:
            try:
                await self._api.aiohttp_session.close()
            except Exception:
                log.exception("error closing aiohttp session")
            self._api = None

    async def _ensure_authenticated(self) -> MyPyllantAPI:
        async with self._login_lock:
            # se ho una sessione valida la riuso
            if self._api is not None:
                expires = getattr(self._api, "oauth_session_expires", None)
                if expires is not None and expires > datetime.now(UTC):
                    return self._api
                # token scaduto/mancante: provo a fare refresh
                try:
                    log.info("Vaillant API: token expired/missing, refreshing")
                    await self._api.refresh_token()
                    return self._api
                except Exception as e:
                    log.warning("Vaillant API: refresh_token failed (%s), full re-login", e)
                    try:
                        await self._api.aiohttp_session.close()
                    except Exception:
                        pass
                    self._api = None

            # full login (solo dopo il successo aggiorno self._api,
            # cosi' non lasciamo lo stato in mezzo se login fallisce)
            log.info("Vaillant API: login")
            api = MyPyllantAPI(self._user, self._password, self._brand, self._country)
            await api.login()
            self._api = api
            expires = getattr(api, "oauth_session_expires", None)
            log.info(
                "Vaillant API: login OK, expires=%s",
                expires.isoformat() if expires else "<None>",
            )
            return self._api

    async def _with_retry(self, coro_factory):
        last_exc: BaseException | None = None
        for attempt in range(1, self._retries + 1):
            try:
                return await coro_factory()
            except aiohttp.ClientResponseError as e:
                # 403 con messaggio "Quota Exceeded": niente retry, surface up
                # subito - il retry non sblocca prima del replenish.
                if e.status == 403 and (
                    "Quota Exceeded" in str(e) or "Out of call volume" in str(e)
                ):
                    match = _QUOTA_REPLENISH_RE.search(str(e))
                    replenish = match.group(1) if match else "<unknown>"
                    log.warning("Vaillant quota exhausted, replenish in %s", replenish)
                    raise VaillantQuotaExceeded(replenish, str(e)[:200]) from e
                # altri 4xx/5xx: tratta come transient -> retry
                last_exc = e
                if attempt < self._retries:
                    wait = self._backoff ** (attempt - 1)
                    log.warning(
                        "Vaillant call failed (attempt %d/%d): %s; retry in %.1fs",
                        attempt, self._retries, e, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    log.error("Vaillant call failed permanently after %d attempts: %s", self._retries, e)
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
                last_exc = e
                if attempt < self._retries:
                    wait = self._backoff ** (attempt - 1)
                    log.warning(
                        "Vaillant call failed (attempt %d/%d): %s; retry in %.1fs",
                        attempt, self._retries, e, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    log.error("Vaillant call failed permanently after %d attempts: %s", self._retries, e)
        assert last_exc is not None
        raise last_exc

    async def _get_cached_system(self, *, force_refresh: bool = False) -> Any:
        """Ritorna il System Vaillant cachato in memoria (o lo fetcha se necessario).

        Singolo flight tramite lock: chiamate concorrenti aspettano la prima.
        TTL configurato da settings.system_cache_ttl_s.
        """
        async with self._system_lock:
            now = time.time()
            if (
                not force_refresh
                and self._system_cache is not None
                and (now - self._system_cache_at) < self._system_cache_ttl
            ):
                return self._system_cache
            api = await self._ensure_authenticated()
            async for system in api.get_systems():
                self._system_cache = system
                self._system_cache_at = now
                return system
            raise RuntimeError("nessun system Vaillant trovato per questo account")

    def invalidate_system_cache(self) -> None:
        """Forza il prossimo _get_cached_system() a rifare fetch.

        Da chiamare dopo una write (PATCH) cosi' il prossimo read vede lo
        stato aggiornato senza dover aspettare il TTL.
        """
        self._system_cache = None
        self._system_cache_at = 0.0

    # Mantieni _first_system come alias per backward-compat dei test
    async def _first_system(self):
        return await self._get_cached_system()

    async def get_water_pressure(self) -> float | None:
        async def _do():
            system = await self._get_cached_system()
            return getattr(system, "water_pressure", None)
        return await self._with_retry(_do)

    async def get_zones(self) -> list[dict[str, Any]]:
        async def _do():
            system = await self._get_cached_system()
            return [{"index": i, "name": z.name} for i, z in enumerate(system.zones)]
        return await self._with_retry(_do)

    async def get_zone_info(self, idx: int) -> dict[str, Any] | None:
        async def _do():
            system = await self._get_cached_system()
            if not (0 <= idx < len(system.zones)):
                return None
            z = system.zones[idx]
            return {
                "index": idx,
                "name": z.name,
                "current_temperature": z.current_room_temperature,
                "desired_temperature": z.desired_room_temperature_setpoint,
                "heating_state": getattr(z.heating, "operation_mode_heating", None),
            }
        return await self._with_retry(_do)

    async def get_zone_flow_temperature(self, idx: int) -> float | None:
        async def _do():
            system = await self._get_cached_system()
            if not (0 <= idx < len(system.zones)):
                return None
            z = system.zones[idx]
            return getattr(z.associated_circuit, "current_circuit_flow_temperature", None)
        return await self._with_retry(_do)

    async def get_gas_consumption(self, year: int, month: int) -> dict[str, Any]:
        """Consumo gas per uno specifico mese. Ritorna breakdown per operation_mode + totale.

        Per il sistema ibrido di Giorgio: DOMESTIC_HOT_WATER + HEATING separati.
        Energia primaria consumata, in m3 (divisore /10000 dalla raw value).
        """
        start = datetime(year, month, 1)
        end = (
            datetime(year + 1, 1, 1) - timedelta(seconds=1)
            if month == 12
            else datetime(year, month + 1, 1) - timedelta(seconds=1)
        )

        async def _do():
            api = await self._ensure_authenticated()
            system = await self._get_cached_system()
            result: dict[str, Any] = {
                "year": year,
                "month": month,
                "by_mode_m3": {},
                "total_m3": 0.0,
            }
            for device in system.devices:
                if device.device_type != "BOILER":
                    continue
                async for data in api.get_data_by_device(
                    device, DeviceDataBucketResolution.MONTH, start, end
                ):
                    if data.energy_type != "CONSUMED_PRIMARY_ENERGY":
                        continue
                    op = data.operation_mode
                    m3 = sum((b.value or 0) / 10000 for b in data.data)
                    result["by_mode_m3"][op] = round(m3, 3)
                    result["total_m3"] += m3
            result["total_m3"] = round(result["total_m3"], 3)
            return result

        return await self._with_retry(_do)

    async def get_gas_consumption_year(self, year: int) -> dict[str, Any]:
        """Consumo gas dell'intero anno: breakdown per mese + per operation_mode + totale.

        Singola chiamata all'API Vaillant con resolution MONTH e range anno intero
        (12 bucket) -> economica rispetto a 12 chiamate separate.
        """
        start = datetime(year, 1, 1)
        end = datetime(year, 12, 31, 23, 59, 59)

        async def _do():
            api = await self._ensure_authenticated()
            system = await self._get_cached_system()
            result: dict[str, Any] = {
                "year": year,
                "by_month": {},       # {1: {by_mode_m3: {...}, total_m3: N}, ...}
                "by_mode_m3": {},     # aggregato annuale per modalita'
                "total_m3": 0.0,
            }
            for device in system.devices:
                if device.device_type != "BOILER":
                    continue
                async for data in api.get_data_by_device(
                    device, DeviceDataBucketResolution.MONTH, start, end
                ):
                    if data.energy_type != "CONSUMED_PRIMARY_ENERGY":
                        continue
                    op = data.operation_mode
                    for bucket in data.data:
                        month = bucket.start_date.month
                        m3 = (bucket.value or 0) / 10000
                        mo = result["by_month"].setdefault(month, {"by_mode_m3": {}, "total_m3": 0.0})
                        mo["by_mode_m3"][op] = round(mo["by_mode_m3"].get(op, 0.0) + m3, 3)
                        mo["total_m3"] = round(mo["total_m3"] + m3, 3)
                        result["by_mode_m3"][op] = round(result["by_mode_m3"].get(op, 0.0) + m3, 3)
                        result["total_m3"] = round(result["total_m3"] + m3, 3)
            return result

        return await self._with_retry(_do)

    async def update_zone_mode(self, idx: int, mode: str) -> dict[str, Any]:
        mode_map = {
            "manual": ZoneOperatingMode.MANUAL,
            "off": ZoneOperatingMode.OFF,
            "time_controlled": ZoneOperatingMode.TIME_CONTROLLED,
        }
        new_mode = mode_map.get(mode.lower())
        if new_mode is None:
            raise ValueError(f"mode invalido: {mode}")

        async def _do():
            # Riusa cache del system: niente get_systems() extra ad ogni write.
            system = await self._get_cached_system()
            if not (0 <= idx < len(system.zones)):
                return {"error": "zone not found"}
            z = system.zones[idx]
            api = await self._ensure_authenticated()
            url = f"{await api.get_system_api_base(z.system_id)}/zones/{z.index}/heating-operation-mode"
            async with api.aiohttp_session.patch(
                url,
                json={"operationMode": new_mode.name},
                headers=api.get_authorized_headers(),
            ) as resp:
                resp.raise_for_status()
            # Invalida cache: prossimo read vede lo stato nuovo.
            self.invalidate_system_cache()
            return {"index": idx, "name": z.name, "mode": new_mode.name}

        return await self._with_retry(_do)

    async def set_zone_setpoint(self, idx: int, temperature: float) -> dict[str, Any]:
        async def _do():
            system = await self._get_cached_system()
            if not (0 <= idx < len(system.zones)):
                return {"error": "zone not found"}
            z = system.zones[idx]
            api = await self._ensure_authenticated()
            await api.set_manual_mode_setpoint(z, temperature, "heating")
            self.invalidate_system_cache()
            return {"index": idx, "name": z.name, "setpoint": temperature}

        return await self._with_retry(_do)

    async def get_system_info(self) -> dict[str, Any]:
        async def _do():
            system = await self._get_cached_system()
            return _serialize(system)

        return await self._with_retry(_do)


def _serialize(obj: Any) -> Any:
    """Serializer "best effort" verso JSON-safe primitives."""

    def default(o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, ZoneInfo):
            return str(o)
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)

    return json.loads(json.dumps(obj, default=default))
