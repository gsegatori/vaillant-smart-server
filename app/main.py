from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.cache import PersistentCache
from app.client import VaillantClient, VaillantQuotaExceeded
from app.config import Settings, get_settings
from app.messages import get_current_language, get_message as _, set_current_language


def _parse_replenish_to_seconds(replenish: str) -> int:
    """Parsa "HH:MM:SS" in secondi totali. Ritorna 0 se non parsabile."""
    try:
        parts = replenish.split(":")
        if len(parts) != 3:
            return 0
        h, m, s = (int(p) for p in parts)
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return 0

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
    set_current_language(settings.language)

    if not settings.vaillant_user or not settings.vaillant_password:
        log.warning(_("log_env_credentials_missing"))

    cache = cache or PersistentCache(settings.cache_file)
    client = client or VaillantClient(
        settings.vaillant_user,
        settings.vaillant_password,
        settings.vaillant_brand,
        settings.vaillant_country,
        retries=settings.retries,
        backoff_base_s=settings.retry_backoff_base_s,
        system_cache_ttl_s=settings.system_cache_ttl_s,
        presets=settings.presets,
    )

    async def _quota_resume_loop() -> None:
        """Background: ogni 30s, se quota_resume_at e' passato, riabilita upstream."""
        while True:
            try:
                await asyncio.sleep(30)
                resume_at = app.state.quota_resume_at
                if resume_at is None:
                    continue
                now = datetime.now(timezone.utc)
                if now >= resume_at:
                    log.info(_("log_quota_window_expired", time=resume_at.strftime("%H:%M:%S")))
                    app.state.enabled = True
                    app.state.quota_resume_at = None
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error in quota_resume_loop")

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        log.info("vaillant-smart-server up bind=%s:%s", settings.bind_host, settings.bind_port)
        resume_task = asyncio.create_task(_quota_resume_loop())
        try:
            yield
        finally:
            log.info("vaillant-smart-server shutdown")
            resume_task.cancel()
            try:
                await resume_task
            except asyncio.CancelledError:
                pass
            await client.close()
            await cache.persist_now()

    app = FastAPI(title="vaillant-smart-server", version="0.2.0", lifespan=_lifespan)
    app.state.settings = settings
    app.state.cache = cache
    app.state.client = client
    app.state.language = get_current_language()
    app.state.enabled = True  # master kill-switch (default ON)
    # Se settato (datetime UTC), un background task riabilitera' upstream a
    # quell'orario. Anche /admin/disable e /admin/enable lo cancellano.
    app.state.quota_resume_at = None  # datetime | None

    # Buffer aggiunto al replenish_in dichiarato da Vaillant: il countdown
    # restituito dall'API e' indicativo, non garantito al secondo. Senza
    # buffer rischiamo di auto-riabilitare proprio mentre Vaillant e' ancora
    # in soglia, e il primo poll del binding HTTP ci ribanna subito.
    QUOTA_RESUME_BUFFER_S = 180  # 3 minuti

    def _trigger_quota_lockout(e: VaillantQuotaExceeded) -> None:
        secs = _parse_replenish_to_seconds(e.replenish_in)
        if secs <= 0:
            log.warning(_("log_quota_lockout_unparseable", replenish=e.replenish_in))
            return
        resume_at = datetime.now(timezone.utc) + timedelta(seconds=secs + QUOTA_RESUME_BUFFER_S)
        was_enabled = app.state.enabled
        app.state.enabled = False
        app.state.quota_resume_at = resume_at
        extra = "" if was_enabled else _("log_was_already_off")
        log.warning(
            _("log_quota_lockout_auto_disable", replenish=e.replenish_in,
              resume=resume_at.strftime("%H:%M:%S"), buffer=QUOTA_RESUME_BUFFER_S, extra=extra)
        )

    def _quota_http_exception(e: VaillantQuotaExceeded) -> HTTPException:
        _trigger_quota_lockout(e)
        return HTTPException(
            status_code=429,
            detail={
                "error": "vaillant_quota_exceeded",
                "replenish_in": e.replenish_in,
                "message": _("quota_exceeded", time=e.replenish_in),
                "vaillant_message": e.message,
            },
        )

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
        """Helper: usa cache normalmente; se master e' OFF, serve solo cache (anche scaduta).

        Su quota Vaillant: la cache serve stale se ce l'ha (gestita da
        PersistentCache.get_or_fetch). Solo se cache vuota propaga 429.
        """
        if not app.state.enabled:
            value = await cache.serve_only(key)
            if value is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"upstream disabled and no cached value for '{key}'",
                )
            return value
        try:
            value, _stale = await cache.get_or_fetch(key, ttl, fetcher)
            return value
        except VaillantQuotaExceeded as e:
            raise _quota_http_exception(e) from e

    # ──────────────────── infra/admin ────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(INDEX_HTML.replace("__SERVER_LANG__", get_current_language()))

    @app.get("/healthz")
    async def healthz():
        body: dict[str, Any] = {"ok": True, "enabled": app.state.enabled}
        resume_at = app.state.quota_resume_at
        if resume_at is not None:
            now = datetime.now(timezone.utc)
            delta_s = max(0, int((resume_at - now).total_seconds()))
            body["quota_resume_at"] = resume_at.isoformat()
            body["quota_resume_in_seconds"] = delta_s
        return body

    @app.get("/admin/cache")
    async def admin_cache():
        return cache.snapshot()

    @app.get("/admin/cache/summary")
    async def admin_cache_summary():
        """Versione compatta dello snapshot, senza i payload value."""
        snap = cache.snapshot()
        return {
            k: {
                "age_seconds": v["age_seconds"],
                "ttl_seconds": v["ttl_seconds"],
                "expired": v["expired"],
            }
            for k, v in snap.items()
        }

    @app.post("/admin/enable")
    async def admin_enable():
        app.state.enabled = True
        if app.state.quota_resume_at is not None:
            app.state.quota_resume_at = None
            log.info(_("log_upstream_enabled_manual"))
        else:
            log.info("upstream ENABLED")
        return {"enabled": True}

    @app.post("/admin/disable")
    async def admin_disable():
        app.state.enabled = False
        # Manuale: cancello eventuale auto-resume cosi' resta OFF finche'
        # l'utente decide diversamente.
        app.state.quota_resume_at = None
        log.info(_("log_upstream_disabled"))
        return {"enabled": False}

    @app.post("/admin/clear-cache")
    async def admin_clear_cache():
        n = cache.clear()
        await cache.persist_now()
        log.info("cache cleared (%d entries)", n)
        return {"cleared_entries": n}

    @app.post("/admin/language/{lang}")
    async def admin_set_language(lang: str):
        lang_lower = lang.lower()
        if lang_lower not in ("en", "it"):
            raise HTTPException(status_code=400, detail="Language must be 'en' or 'it'")
        set_current_language(lang_lower)
        app.state.language = lang_lower
        log.info("Language changed to %s", lang_lower)
        return {"ok": True, "language": lang_lower}

    @app.get("/admin/config")
    async def admin_config():
        """Dump della config runtime (senza la password). Utile per debug
        rapido per capire QUALI TTL e parametri stanno effettivamente girando."""
        d = settings.model_dump()
        if "vaillant_password" in d:
            d["vaillant_password"] = "***" if d["vaillant_password"] else "(empty)"
        # serializza Path come str per JSON
        for k, v in list(d.items()):
            if hasattr(v, "__fspath__"):
                d[k] = str(v)
        return d

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
        except VaillantQuotaExceeded as e:
            raise _quota_http_exception(e) from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Optimistic update: la PATCH e' confermata, sappiamo per certo il
        # nuovo mode -> aggiorno la cache zone_info cosi' il prossimo poll di
        # OH lo vede subito (cache hit, niente chiamata Vaillant).
        new_mode = result.get("mode")
        if new_mode and not cache.patch_value(f"zone_info_{idx}", heating_state=new_mode):
            cache.invalidate(f"zone_info_{idx}")  # entry non c'era: refetch al prossimo accesso
        await cache.persist_now()
        return result

    @app.get("/zone-set-temp/{idx}/{temp}")
    async def zone_set_temp(idx: int, temp: float):
        if not app.state.enabled:
            raise HTTPException(status_code=503, detail="upstream disabled")
        try:
            result = await client.set_zone_setpoint(idx, temp)
        except VaillantQuotaExceeded as e:
            raise _quota_http_exception(e) from e
        # Optimistic update del setpoint nella cache zone_info.
        if not cache.patch_value(f"zone_info_{idx}", desired_temperature=temp):
            cache.invalidate(f"zone_info_{idx}")
        await cache.persist_now()
        return result

    @app.get("/dhw-set-temp/{idx}/{temp}")
    async def dhw_set_temp(idx: int, temp: float):
        if not app.state.enabled:
            raise HTTPException(status_code=503, detail="upstream disabled")
        try:
            result = await client.update_dhw_temperature(idx, temp)
        except VaillantQuotaExceeded as e:
            raise _quota_http_exception(e) from e
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        # Optimistic update of DHW setpoint in cache, or invalidate if not cached.
        if not cache.patch_value(f"dhw_info_{idx}", desired_temperature=temp, temperature=temp):
            cache.invalidate(f"dhw_info_{idx}")
        cache.invalidate("system_info")
        cache.invalidate("system_info_and_data")
        await cache.persist_now()
        return result

    @app.get("/apply-preset/{preset}")
    async def apply_preset(preset: str):
        if not app.state.enabled:
            raise HTTPException(status_code=503, detail="upstream disabled")
        preset_upper = preset.upper()
        available_presets = getattr(client, "presets", settings.presets)
        if preset_upper not in available_presets:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid mode '{preset}'. Valid modes: {list(available_presets.keys())}",
            )
        try:
            return await client.apply_preset(preset_upper)
        except VaillantQuotaExceeded as e:
            raise _quota_http_exception(e) from e

    @app.get("/get-water-pressure")
    async def get_water_pressure():
        value = await _cached("water_pressure", settings.cache_ttl_water_pressure, client.get_water_pressure)
        return {"pressure": value}

    @app.get("/get-system-info")
    async def get_system_info():
        return await _cached("system_info", settings.cache_ttl_system_info, client.get_system_info)

    @app.get("/get-system-info-and-data")
    async def get_system_info_and_data():
        return await _cached(
            "system_info_and_data",
            settings.cache_ttl_system_info_and_data,
            client.get_system_info_and_data,
        )

    @app.get("/get-device-data")
    async def get_device_data():
        return await _cached("device_data", settings.cache_ttl_device_data, client.get_device_data)

    @app.get("/get-rts")
    async def get_rts():
        return await _cached("rts", settings.cache_ttl_rts, client.get_rts)

    return app


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title id="page-title">Vaillant Smart Server</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; background: #fafafa; }
  .header-row { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; margin-bottom: .4em; }
  h1 { margin: 0; }
  h1 small { color: #888; font-size: .55em; font-weight: normal; }
  .lang-bar { display: flex; gap: .4em; align-items: center; }
  .lang-bar button { padding: .35em .75em; font-size: .88em; border: 1px solid #ccc; background: #fff; cursor: pointer; border-radius: 4px; }
  .lang-bar button.active { background: #2c7; color: white; border-color: #2a6; font-weight: 600; }
  h2 { border-bottom: 1px solid #ddd; padding-bottom: .3em; margin-top: 1.5em; }
  .panel { background: white; padding: 1em 1.2em; border-radius: 8px; margin: 1em 0;
           box-shadow: 0 1px 3px rgba(0,0,0,.05); }
  button { padding: .5em 1em; margin: .15em .3em .15em 0; border: 1px solid #ccc;
           background: white; cursor: pointer; border-radius: 5px; font-size: .92em; }
  button:hover:not(:disabled) { background: #f0f0f0; }
  button:disabled { opacity: .4; cursor: not-allowed; }
  button.primary { background: #2c7; color: white; border-color: #2a6; }
  button.primary:hover:not(:disabled) { background: #2a6; }
  button.danger { color: #c00; border-color: #faa; }
  button.danger:hover:not(:disabled) { background: #fee; }
  .pill { display: inline-block; padding: .15em .65em; border-radius: 100px;
          font-size: .85em; font-weight: 600; }
  .pill.on, .pill.fresh { background: #d4f5d4; color: #060; }
  .pill.off, .pill.expired { background: #fcd; color: #900; }
  .pill.unknown { background: #ddd; color: #555; }
  .quota-banner { background: #ffe4e4; border: 1px solid #f99; color: #900;
                  padding: .8em 1em; border-radius: 5px; margin: 1em 0;
                  display: none; font-weight: 600; }
  .quota-banner.show { display: block; }
  .zone-card { background: #f5f5f7; border-radius: 6px; padding: .9em 1em;
               margin: .6em 0; }
  .zone-card h3 { margin: 0 0 .4em; font-size: 1.05em; color: #333; }
  .zone-row { display: flex; flex-wrap: wrap; gap: 1.2em; margin: .2em 0; font-size: .92em; }
  .zone-row .label { color: #777; font-size: .85em; display: block; }
  .zone-row .val { font-weight: 600; }
  .zone-actions { margin-top: .6em; }
  .zone-actions input[type=number] { width: 5em; padding: .35em; border: 1px solid #ccc;
                                     border-radius: 4px; font-size: .92em; }
  table { border-collapse: collapse; width: 100%; margin-top: .5em; }
  th, td { padding: .45em .7em; border-bottom: 1px solid #eee; text-align: left; font-size: .9em; }
  th { background: #f5f5f7; font-weight: 600; }
  td code { background: #f4f4f6; padding: 1px 6px; border-radius: 3px; font-size: .9em; }
  pre { background: #1e1e1e; color: #ddd; padding: 1em; border-radius: 5px;
        overflow-x: auto; font-size: .82em; max-height: 320px; }
  small.muted { color: #888; }
</style>
</head>
<body>

<div class="header-row">
  <h1>Vaillant Smart Server <small>v0.2.0</small></h1>
  <div class="lang-bar">
    <button id="lang-btn-it" onclick="setLanguage('it')">&#127470;&#127481; Italiano</button>
    <button id="lang-btn-en" onclick="setLanguage('en')">&#127468;&#127463; English</button>
  </div>
</div>

<div id="quota-banner" class="quota-banner"></div>

<div class="panel">
  <h2 id="status-heading">Status</h2>
  <p>
    <span id="master-label">Master upstream:</span>
    <span id="master-state" class="pill unknown">loading...</span>
    <button id="btn-enable" class="primary" onclick="enableMaster()">Enable</button>
    <button id="btn-disable" class="danger" onclick="disableMaster()">Disable</button>
  </p>
  <p><small class="muted" id="status-subtext"></small></p>
</div>

<div class="panel">
  <h2 id="presets-heading">Modes and Presets</h2>
  <p>
    <span id="presets-desc">Activate heating / DHW program:</span>
    <button id="btn-preset-at-home" onclick="applyPreset('AT_HOME')">At Home</button>
    <button id="btn-preset-holiday" onclick="applyPreset('HOLIDAY')">Holiday</button>
    <button id="btn-preset-coming-back" onclick="applyPreset('COMING_BACK')">Coming Back</button>
  </p>
</div>

<div class="panel">
  <h2 id="dhw-heading">Domestic Hot Water (DHW)</h2>
  <p>
    <span id="dhw-label">Set temperature:</span>
    <input type="number" id="dhw-temp-input" value="50" min="35" max="65" step="1" style="width: 4.5em; padding: .35em; border: 1px solid #ccc; border-radius: 4px;">&deg;C
    <button id="btn-set-dhw" onclick="setDhwTemp(0)">Set DHW</button>
  </p>
</div>

<div class="panel">
  <h2 id="zones-heading">Zones</h2>
  <button id="btn-reload-zones" onclick="refreshZones()">&#8635; Reload</button>
  <div id="zones-list"><em>loading...</em></div>
  <p><small class="muted" id="zones-subtext"></small></p>
</div>

<div class="panel">
  <h2 id="cache-heading">Cache</h2>
  <button id="btn-reload-cache" onclick="refreshCache()">&#8635; Reload</button>
  <button id="btn-clear-cache" class="danger" onclick="clearCache()">&#128465; Clear cache</button>
  <table id="cache-table">
    <thead><tr><th id="th-key">Key</th><th id="th-age">Age</th><th id="th-ttl">TTL</th><th id="th-status">Status</th></tr></thead>
    <tbody><tr><td colspan="4"><em>loading...</em></td></tr></tbody>
  </table>
  <p><small class="muted" id="cache-subtext"></small></p>
</div>

<div class="panel">
  <h2 id="probe-heading">Probe endpoints</h2>
  <p>
    <button onclick="probe('/zone-info/0')">/zone-info/0</button>
    <button onclick="probe('/zone-info/1')">/zone-info/1</button>
    <button onclick="probe('/zone-info/2')">/zone-info/2</button>
    <button onclick="probe('/dhw-set-temp/0/55')">/dhw-set-temp/0/55</button>
    <button onclick="probe('/get-water-pressure')">/get-water-pressure</button>
    <button onclick="probe('/boiler-consumption-current-month')">/boiler-consumption-current-month</button>
    <button onclick="probe('/boiler-consumption-current-year')">/boiler-consumption-current-year</button>
    <button onclick="probe('/get-system-info')">/get-system-info</button>
    <button onclick="probe('/get-system-info-and-data')">/get-system-info-and-data</button>
    <button onclick="probe('/get-device-data')">/get-device-data</button>
    <button onclick="probe('/get-rts')">/get-rts</button>
  </p>
  <pre id="probe-out">(click an endpoint to see its response)</pre>
</div>

<script>
const I18N = {
  en: {
    title: "Vaillant Smart Server",
    quotaBannerHtml: '&#9888; Vaillant API quota exceeded. Auto-resume in <span id="quota-time">--:--:--</span>. Upstream has been automatically disabled (OFF); the server will automatically re-enable when quota is replenished. You can manually force Enable or keep it OFF (Disable) \u2014 both cancel the auto-resume timer.',
    quotaProbeHeader: '\\n\\n\\u26A0 VAILLANT QUOTA EXCEEDED. Replenish in ',
    statusHeading: "Status",
    masterUpstream: "Master upstream:",
    btnEnable: "Enable",
    btnDisable: "Disable",
    statusSubtext: "When OFF, the server only serves from cache (even expired) and <strong>does not call Vaillant</strong>. Use it to free up the API while using the official myVaillant app or when quota is exceeded.",
    presetsHeading: "Modes and Presets",
    presetsDesc: "Activate heating / DHW program:",
    btnAtHome: "At Home",
    btnHoliday: "Holiday",
    btnComingBack: "Coming Back",
    dhwHeading: "Domestic Hot Water (DHW)",
    dhwLabel: "Set temperature:",
    btnSetDhw: "Set DHW",
    zonesHeading: "Zones",
    btnReload: "&#8635; Reload",
    zonesSubtext: "These are the exact values returned by the server. If OpenHAB shows something different, the HTTP binding has not completed a refresh yet \u2014 it will sync within 30s.",
    cacheHeading: "Cache",
    btnClearCache: "&#128465; Clear cache",
    colKey: "Key",
    colAge: "Age",
    colTtl: "TTL",
    colStatus: "Status",
    cacheEmpty: "empty cache",
    statusFresh: "fresh",
    statusExpired: "expired",
    cacheSubtext: 'Expired entries are still served (serve-stale), but on next access the server will attempt a fresh fetch from Vaillant.',
    probeHeading: "Probe endpoints",
    probeHint: "(click an endpoint to see its response)",
    emptyBody: "(empty body)",
    labelTemp: "Temperature",
    labelSetpoint: "Setpoint",
    labelMode: "Mode",
    modeOff: "Off",
    modeManual: "Manual",
    modeSchedule: "Schedule",
    btnSet: "Set",
    manualRequired: "(requires Manual mode)",
    loading: "loading...",
    unreachable: "unreachable",
    confirmClearCache: "Clear ENTIRE cache? Subsequent calls will fetch fresh data from Vaillant.",
    confirmPreset: function(mode) { return 'Activate preset ' + mode + '?'; },
    alertPresetSuccess: function(mode) { return 'Preset ' + mode + ' applied successfully!'; },
    alertDhwSuccess: function(val) { return 'DHW temperature set to ' + val + '\u00B0C'; },
    alertInvalidTemp: function(val) { return 'Invalid temperature: ' + val; },
    alertInvalidSetpoint: function(val) { return 'Invalid setpoint: ' + val; },
    alertError: function(status, body) { return 'Error ' + status + ': ' + JSON.stringify(body); },
    unexpectedZones: function(zs) { return 'Unexpected response from /zones: ' + JSON.stringify(zs); },
    zoneFallback: function(idx) { return 'Zone ' + idx; },
    errorPrefix: "error: "
  },
  it: {
    title: "Vaillant Smart Server",
    quotaBannerHtml: '&#9888; Vaillant ha esaurito la quota API. Auto-resume tra <span id="quota-time">--:--:--</span>. L\\\'upstream e\\\' stato messo OFF automaticamente; al replenish il server ri-abilita da solo. Manualmente puoi forzare l\\\'abilitazione (Abilita) o tenerla OFF (Disabilita) \u2014 entrambi annullano il timer auto-resume.',
    quotaProbeHeader: '\\n\\n\\u26A0 VAILLANT QUOTA ESAURITA. Replenish in ',
    statusHeading: "Stato",
    masterUpstream: "Master upstream:",
    btnEnable: "Abilita",
    btnDisable: "Disabilita",
    statusSubtext: "Quando OFF, il server serve solo dalla cache (anche scaduta) e <strong>non chiama Vaillant</strong>. Usalo per liberare l'API mentre usi l'app myVaillant ufficiale o quando hai sforato la quota.",
    presetsHeading: "Modalit\u00E0 e Preset",
    presetsDesc: "Attiva programma riscaldamento / ACS:",
    btnAtHome: "A Casa",
    btnHoliday: "Vacanza",
    btnComingBack: "Rientro",
    dhwHeading: "Acqua Calda Sanitaria (ACS)",
    dhwLabel: "Imposta temperatura:",
    btnSetDhw: "Imposta ACS",
    zonesHeading: "Zone",
    btnReload: "&#8635; Ricarica",
    zonesSubtext: "Questi sono i valori esatti che il server ritorna. Se OH mostra qualcosa di diverso, e' il binding HTTP che non ha ancora finito un refresh \u2014 entro 30s sara' allineato.",
    cacheHeading: "Cache",
    btnClearCache: "&#128465; Svuota cache",
    colKey: "Chiave",
    colAge: "Eta'",
    colTtl: "TTL",
    colStatus: "Stato",
    cacheEmpty: "cache vuota",
    statusFresh: "fresca",
    statusExpired: "scaduta",
    cacheSubtext: 'Le entry "scadute" sono ancora servite (serve-stale) ma al prossimo accesso il server tenta un fetch fresh da Vaillant.',
    probeHeading: "Endpoint probe",
    probeHint: "(clicca un endpoint per vederne la risposta)",
    emptyBody: "(body vuoto)",
    labelTemp: "Temperatura",
    labelSetpoint: "Setpoint",
    labelMode: "Modalit\u00E0",
    modeOff: "Off",
    modeManual: "Manuale",
    modeSchedule: "Programma",
    btnSet: "Imposta",
    manualRequired: "(richiede modalit\u00E0 Manuale)",
    loading: "caricamento...",
    unreachable: "non raggiungibile",
    confirmClearCache: "Svuotare TUTTA la cache? Le prossime chiamate andranno fresh a Vaillant.",
    confirmPreset: function(mode) { return 'Attivare preset ' + mode + '?'; },
    alertPresetSuccess: function(mode) { return 'Preset ' + mode + ' applicato con successo!'; },
    alertDhwSuccess: function(val) { return 'Temperatura ACS impostata a ' + val + '\u00B0C'; },
    alertInvalidTemp: function(val) { return 'Temperatura non valida: ' + val; },
    alertInvalidSetpoint: function(val) { return 'Setpoint non valido: ' + val; },
    alertError: function(status, body) { return 'Errore ' + status + ': ' + JSON.stringify(body); },
    unexpectedZones: function(zs) { return 'Risposta inattesa da /zones: ' + JSON.stringify(zs); },
    zoneFallback: function(idx) { return 'Zona ' + idx; },
    errorPrefix: "errore: "
  }
};

const SERVER_LANG = "__SERVER_LANG__";
let currentLang = "en";
try {
  currentLang = localStorage.getItem('vaillant_lang') || (SERVER_LANG === 'it' ? 'it' : 'en');
} catch (e) {
  currentLang = (SERVER_LANG === 'it') ? 'it' : 'en';
}

function applyLanguageUI() {
  document.documentElement.lang = currentLang;

  const btnIt = document.getElementById('lang-btn-it');
  const btnEn = document.getElementById('lang-btn-en');
  if (btnIt && btnEn) {
    btnIt.className = (currentLang === 'it') ? 'active' : '';
    btnEn.className = (currentLang === 'en') ? 'active' : '';
  }

  const t = I18N[currentLang];
  const setTxt = function(id, txt) { const el = document.getElementById(id); if (el) el.textContent = txt; };
  const setHtml = function(id, html) { const el = document.getElementById(id); if (el) el.innerHTML = html; };

  setTxt('page-title', t.title);
  setTxt('status-heading', t.statusHeading);
  setTxt('master-label', t.masterUpstream);
  setTxt('btn-enable', t.btnEnable);
  setTxt('btn-disable', t.btnDisable);
  setHtml('status-subtext', t.statusSubtext);

  setTxt('presets-heading', t.presetsHeading);
  setTxt('presets-desc', t.presetsDesc);
  setTxt('btn-preset-at-home', t.btnAtHome);
  setTxt('btn-preset-holiday', t.btnHoliday);
  setTxt('btn-preset-coming-back', t.btnComingBack);

  setTxt('dhw-heading', t.dhwHeading);
  setTxt('dhw-label', t.dhwLabel);
  setTxt('btn-set-dhw', t.btnSetDhw);

  setTxt('zones-heading', t.zonesHeading);
  setHtml('btn-reload-zones', t.btnReload);
  setHtml('zones-subtext', t.zonesSubtext);

  setTxt('cache-heading', t.cacheHeading);
  setHtml('btn-reload-cache', t.btnReload);
  setHtml('btn-clear-cache', t.btnClearCache);
  setTxt('th-key', t.colKey);
  setTxt('th-age', t.colAge);
  setTxt('th-ttl', t.colTtl);
  setTxt('th-status', t.colStatus);
  setHtml('cache-subtext', t.cacheSubtext);

  setTxt('probe-heading', t.probeHeading);
  const probeOut = document.getElementById('probe-out');
  if (probeOut && (probeOut.textContent.startsWith('(') || probeOut.textContent === '')) {
    probeOut.textContent = t.probeHint;
  }

  const banner = document.getElementById('quota-banner');
  if (banner && banner.classList.contains('show')) {
    const qTimeEl = document.getElementById('quota-time');
    const curTime = qTimeEl ? qTimeEl.textContent : '--:--:--';
    banner.innerHTML = t.quotaBannerHtml;
    const newTime = document.getElementById('quota-time');
    if (newTime) newTime.textContent = curTime;
  }
}

function setLanguage(lang) {
  currentLang = (lang === 'it') ? 'it' : 'en';
  try { localStorage.setItem('vaillant_lang', currentLang); } catch (e) {}
  fetch('/admin/language/' + currentLang, { method: 'POST' }).catch(function() {});
  applyLanguageUI();
  refreshStatus();
  refreshCache();
  refreshZones();
}

function formatHMS(totalSec) {
  totalSec = Math.max(0, Math.floor(totalSec));
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
}

async function refreshStatus() {
  const t = I18N[currentLang];
  try {
    const r = await fetch('/healthz');
    const d = await r.json();
    const el = document.getElementById('master-state');
    if (d.enabled) { el.textContent = 'ON';  el.className = 'pill on'; }
    else            { el.textContent = 'OFF'; el.className = 'pill off'; }

    // Update auto-resume banner
    if (typeof d.quota_resume_in_seconds === 'number' && d.quota_resume_in_seconds > 0) {
      showQuotaBanner(formatHMS(d.quota_resume_in_seconds));
    } else {
      hideQuotaBanner();
    }
  } catch (e) {
    document.getElementById('master-state').textContent = t.unreachable;
  }
}
async function enableMaster()  { await fetch('/admin/enable',  { method: 'POST' }); refreshStatus(); }
async function disableMaster() { await fetch('/admin/disable', { method: 'POST' }); refreshStatus(); }

async function refreshCache() {
  const t = I18N[currentLang];
  try {
    const r = await fetch('/admin/cache');
    const d = await r.json();
    const tbody = document.querySelector('#cache-table tbody');
    const keys = Object.keys(d).sort();
    if (keys.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4"><em>' + t.cacheEmpty + '</em></td></tr>';
      return;
    }
    tbody.innerHTML = keys.map(function(k) {
      const v = d[k];
      const cls = v.expired ? 'expired' : 'fresh';
      const txt = v.expired ? t.statusExpired : t.statusFresh;
      return '<tr>' +
        '<td><code>' + k + '</code></td>' +
        '<td>' + Math.round(v.age_seconds) + 's</td>' +
        '<td>' + v.ttl_seconds + 's</td>' +
        '<td><span class="pill ' + cls + '">' + txt + '</span></td>' +
      '</tr>';
    }).join('');
  } catch (e) {
    document.querySelector('#cache-table tbody').innerHTML =
      '<tr><td colspan="4">' + t.errorPrefix + e.message + '</td></tr>';
  }
}

async function clearCache() {
  const t = I18N[currentLang];
  if (!confirm(t.confirmClearCache)) return;
  await fetch('/admin/clear-cache', { method: 'POST' });
  refreshCache();
}

function showQuotaBanner(replenishIn) {
  const el = document.getElementById('quota-banner');
  const t = I18N[currentLang];
  el.innerHTML = t.quotaBannerHtml;
  const timeSpan = document.getElementById('quota-time');
  if (timeSpan) timeSpan.textContent = replenishIn || '--:--:--';
  el.classList.add('show');
}
function hideQuotaBanner() {
  document.getElementById('quota-banner').classList.remove('show');
}

async function handleResponse(r) {
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch (e) { body = text; }
  let quotaReplenish = null;
  if (r.status === 429 && body && body.detail && body.detail.replenish_in) {
    quotaReplenish = body.detail.replenish_in;
    showQuotaBanner(quotaReplenish);
  } else if (r.ok) {
    hideQuotaBanner();
  }
  return { ok: r.ok, status: r.status, body: body, quotaReplenish: quotaReplenish };
}

const MODE_PILL = { OFF: 'off', MANUAL: 'on', TIME_CONTROLLED: 'fresh' };

async function refreshZones() {
  const t = I18N[currentLang];
  const out = document.getElementById('zones-list');
  try {
    const zsR = await fetch('/zones');
    const zs = await zsR.json();
    if (!Array.isArray(zs)) {
      out.textContent = t.unexpectedZones(zs);
      return;
    }
    const infos = await Promise.all(zs.map(function(z) {
      return fetch('/zone-info/' + z.index).then(function(r) { return r.json(); });
    }));
    out.innerHTML = zs.map(function(z, i) { return renderZone(z, infos[i]); }).join('');
  } catch (e) {
    out.textContent = t.errorPrefix + e.message;
  }
}

function renderZone(z, info) {
  const t = I18N[currentLang];
  const idx = z.index;
  const rawMode = (info && info.heating_state) || '?';
  const modeLower = String(rawMode).toLowerCase();
  const pillCls = MODE_PILL[String(rawMode).toUpperCase()] || 'unknown';
  let modeLabel = rawMode;
  if (modeLower === 'off') modeLabel = t.modeOff;
  else if (modeLower === 'manual') modeLabel = t.modeManual;
  else if (modeLower === 'time_controlled') modeLabel = t.modeSchedule;

  const curT = info && typeof info.current_temperature === 'number'
    ? info.current_temperature.toFixed(2) + ' \u00B0C' : '?';
  const spT  = info && typeof info.desired_temperature === 'number'
    ? info.desired_temperature.toFixed(1) + ' \u00B0C' : '?';
  const name = z.name || t.zoneFallback(idx);
  const spVal = (info && typeof info.desired_temperature === 'number') ? info.desired_temperature : 20;

  return `
    <div class="zone-card" id="zone-${idx}">
      <h3>${name} <small class="muted">[idx=${idx}]</small></h3>
      <div class="zone-row">
        <div><span class="label">${t.labelTemp}</span><span class="val">${curT}</span></div>
        <div><span class="label">${t.labelSetpoint}</span><span class="val">${spT}</span></div>
        <div><span class="label">${t.labelMode}</span><span class="pill ${pillCls}">${modeLabel}</span></div>
      </div>
      <div class="zone-actions">
        ${t.labelMode}:
        <button onclick="setZoneMode(${idx}, 'off')">${t.modeOff}</button>
        <button onclick="setZoneMode(${idx}, 'manual')">${t.modeManual}</button>
        <button onclick="setZoneMode(${idx}, 'time_controlled')">${t.modeSchedule}</button>
      </div>
      <div class="zone-actions">
        ${t.labelSetpoint}:
        <input type="number" id="sp-${idx}" value="${spVal}" step="0.5" min="5" max="30">&deg;C
        <button onclick="setZoneSetpoint(${idx})">${t.btnSet}</button>
        <small class="muted">${t.manualRequired}</small>
      </div>
    </div>
  `;
}

async function setZoneMode(idx, mode) {
  const t = I18N[currentLang];
  const r = await fetch('/zone-update/' + idx + '/' + mode);
  const res = await handleResponse(r);
  if (!res.ok) {
    alert(t.alertError(res.status, res.body));
    return;
  }
  setTimeout(refreshZones, 500);
  setTimeout(refreshCache, 800);
}

async function setZoneSetpoint(idx) {
  const t = I18N[currentLang];
  const val = document.getElementById('sp-' + idx).value;
  if (!val || isNaN(parseFloat(val))) {
    alert(t.alertInvalidSetpoint(val));
    return;
  }
  const r = await fetch('/zone-set-temp/' + idx + '/' + val);
  const res = await handleResponse(r);
  if (!res.ok) {
    alert(t.alertError(res.status, res.body));
    return;
  }
  setTimeout(refreshZones, 500);
  setTimeout(refreshCache, 800);
}

async function applyPreset(mode) {
  const t = I18N[currentLang];
  if (!confirm(t.confirmPreset(mode))) return;
  const r = await fetch('/apply-preset/' + mode);
  const res = await handleResponse(r);
  if (!res.ok) {
    alert(t.alertError(res.status, res.body));
    return;
  }
  alert(t.alertPresetSuccess(mode));
  setTimeout(refreshZones, 500);
  setTimeout(refreshCache, 800);
}

async function setDhwTemp(idx) {
  const t = I18N[currentLang];
  const val = document.getElementById('dhw-temp-input').value;
  if (!val || isNaN(parseFloat(val))) {
    alert(t.alertInvalidTemp(val));
    return;
  }
  const r = await fetch('/dhw-set-temp/' + idx + '/' + val);
  const res = await handleResponse(r);
  if (!res.ok) {
    alert(t.alertError(res.status, res.body));
    return;
  }
  alert(t.alertDhwSuccess(val));
  setTimeout(refreshCache, 800);
}

async function probe(path) {
  const t = I18N[currentLang];
  const out = document.getElementById('probe-out');
  out.textContent = 'GET ' + path + '\\n...';
  const t0 = performance.now();
  try {
    const r = await fetch(path);
    const dt = Math.round(performance.now() - t0);
    const text = await r.text();
    let pretty;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); }
    catch (e) { pretty = text || t.emptyBody; }

    let header = 'GET ' + path + '\\nHTTP ' + r.status + '  in ' + dt + 'ms';
    if (r.status === 429) {
      try {
        const d = JSON.parse(text);
        const replenish = d.detail && d.detail.replenish_in;
        if (replenish) {
          showQuotaBanner(replenish);
          header += t.quotaProbeHeader + replenish;
        }
      } catch (e) {}
    } else if (r.status === 200 && path.startsWith('/zone-info')) {
      hideQuotaBanner();
    }

    out.textContent = header + '\\n\\n' + pretty;
    refreshCache();
  } catch (e) {
    out.textContent = t.errorPrefix + e.message;
  }
}

if (currentLang !== SERVER_LANG) {
  fetch('/admin/language/' + currentLang, { method: 'POST' }).catch(function() {});
}
applyLanguageUI();
refreshStatus();
refreshCache();
refreshZones();
setInterval(refreshStatus, 10000);
setInterval(refreshCache, 30000);
setInterval(refreshZones, 30000);
</script>
</body>
</html>
"""

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
