from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.cache import PersistentCache
from app.client import VaillantClient
from app.config import Settings, get_settings

log = logging.getLogger("vaillant")


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    if level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "aiohttp.client", "aiohttp.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def create_app(
    settings: Settings | None = None,
    client: VaillantClient | None = None,
    cache: PersistentCache | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    if not settings.vaillant_user or not settings.vaillant_password:
        log.warning("VAILLANT_USER o VAILLANT_PASSWORD non impostati nel .env")

    cache = cache or PersistentCache(settings.cache_file)
    client = client or VaillantClient(
        settings.vaillant_user,
        settings.vaillant_password,
        settings.vaillant_brand,
        settings.vaillant_country,
        retries=settings.retries,
        backoff_base_s=settings.retry_backoff_base_s,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        log.info("vaillant-smart-server up bind=%s:%s", settings.bind_host, settings.bind_port)
        try:
            yield
        finally:
            log.info("vaillant-smart-server shutdown")
            await client.close()
            await cache.persist_now()

    app = FastAPI(title="vaillant-smart-server", version="0.2.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.cache = cache
    app.state.client = client
    app.state.enabled = True  # master kill-switch (default ON)

    @app.middleware("http")
    async def _access_log(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if response.status_code >= 400:
            log.warning("%s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, dt_ms)
        else:
            log.info("%s %s -> %d (%.1fms)", request.method, request.url.path, response.status_code, dt_ms)
        return response

    async def _cached(
        key: str, ttl: int, fetcher: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Helper: usa cache normalmente; se master e' OFF, serve solo cache (anche scaduta)."""
        if not app.state.enabled:
            value = await cache.serve_only(key)
            if value is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"upstream disabled and no cached value for '{key}'",
                )
            return value
        value, _stale = await cache.get_or_fetch(key, ttl, fetcher)
        return value

    # ──────────────────── infra/admin ────────────────────

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "enabled": app.state.enabled}

    @app.get("/admin/cache")
    async def admin_cache():
        return cache.snapshot()

    @app.post("/admin/enable")
    async def admin_enable():
        app.state.enabled = True
        log.info("upstream ENABLED")
        return {"enabled": True}

    @app.post("/admin/disable")
    async def admin_disable():
        app.state.enabled = False
        log.info("upstream DISABLED (cache-only mode)")
        return {"enabled": False}

    # ──────────────────── endpoint compat OH (URL legacy) ────────────────────

    @app.get("/boiler-consumption/{year}/{month}")
    async def boiler_consumption(year: int, month: int):
        return await _cached(
            f"gas_{year}_{month:02d}",
            settings.cache_ttl_gas,
            lambda: client.get_gas_consumption(year, month),
        )

    @app.get("/boiler-consumption-current-month")
    async def boiler_consumption_current():
        now = datetime.now()
        return await boiler_consumption(now.year, now.month)

    @app.get("/boiler-consumption-year/{year}")
    async def boiler_consumption_year(year: int):
        return await _cached(
            f"gas_year_{year}",
            settings.cache_ttl_gas,
            lambda: client.get_gas_consumption_year(year),
        )

    @app.get("/boiler-consumption-current-year")
    async def boiler_consumption_current_year():
        now = datetime.now()
        return await boiler_consumption_year(now.year)

    @app.get("/zones")
    async def zones_list():
        return await _cached("zones", settings.cache_ttl_zones, client.get_zones)

    @app.get("/zone-info/{idx}")
    async def zone_info(idx: int):
        value = await _cached(
            f"zone_info_{idx}",
            settings.cache_ttl_zone_info,
            lambda: client.get_zone_info(idx),
        )
        if value is None:
            raise HTTPException(status_code=404, detail="zone not found")
        return value

    @app.get("/zone-flow-temp/{idx}")
    async def zone_flow_temp(idx: int):
        value = await _cached(
            f"zone_flow_temp_{idx}",
            settings.cache_ttl_zone_flow,
            lambda: client.get_zone_flow_temperature(idx),
        )
        if value is None:
            raise HTTPException(status_code=404, detail="zone or flow temp not available")
        return {"flow_temperature": value}

    @app.get("/zone-update/{idx}/{mode}")
    async def zone_update(idx: int, mode: str):
        if not app.state.enabled:
            raise HTTPException(status_code=503, detail="upstream disabled")
        try:
            result = await client.update_zone_mode(idx, mode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        cache.invalidate(f"zone_info_{idx}")
        return result

    @app.get("/zone-set-temp/{idx}/{temp}")
    async def zone_set_temp(idx: int, temp: float):
        if not app.state.enabled:
            raise HTTPException(status_code=503, detail="upstream disabled")
        result = await client.set_zone_setpoint(idx, temp)
        cache.invalidate(f"zone_info_{idx}")
        return result

    @app.get("/get-water-pressure")
    async def get_water_pressure():
        value = await _cached("water_pressure", settings.cache_ttl_water_pressure, client.get_water_pressure)
        return {"pressure": value}

    @app.get("/get-system-info")
    async def get_system_info():
        return await _cached("system_info", settings.cache_ttl_system_info, client.get_system_info)

    return app


def get_app() -> FastAPI:
    return create_app()


def main() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:get_app",
        factory=True,
        host=s.bind_host,
        port=s.bind_port,
        workers=1,
        log_level=s.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
