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
        system_cache_ttl_s=settings.system_cache_ttl_s,
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
                    log.info(
                        "Quota window scaduta (era %s), AUTO-ENABLE upstream",
                        resume_at.strftime("%H:%M:%S"),
                    )
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
            log.warning("Quota lockout senza replenish_in parsabile (%s); upstream lasciato com'e'", e.replenish_in)
            return
        resume_at = datetime.now(timezone.utc) + timedelta(seconds=secs + QUOTA_RESUME_BUFFER_S)
        was_enabled = app.state.enabled
        app.state.enabled = False
        app.state.quota_resume_at = resume_at
        log.warning(
            "Auto-DISABLE upstream per quota Vaillant (Vaillant dice %s, riprendo alle %s UTC con +%ds buffer)%s",
            e.replenish_in, resume_at.strftime("%H:%M:%S"), QUOTA_RESUME_BUFFER_S,
            "" if was_enabled else " (era gia' OFF)",
        )

    def _quota_http_exception(e: VaillantQuotaExceeded) -> HTTPException:
        _trigger_quota_lockout(e)
        return HTTPException(
            status_code=429,
            detail={
                "error": "vaillant_quota_exceeded",
                "replenish_in": e.replenish_in,
                "message": f"Quota API Vaillant esaurita, replenish in {e.replenish_in}",
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
        return INDEX_HTML

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

    @app.post("/admin/enable")
    async def admin_enable():
        app.state.enabled = True
        if app.state.quota_resume_at is not None:
            app.state.quota_resume_at = None
            log.info("upstream ENABLED (manual override del quota auto-resume)")
        else:
            log.info("upstream ENABLED")
        return {"enabled": True}

    @app.post("/admin/disable")
    async def admin_disable():
        app.state.enabled = False
        # Manuale: cancello eventuale auto-resume cosi' resta OFF finche'
        # l'utente decide diversamente.
        app.state.quota_resume_at = None
        log.info("upstream DISABLED (cache-only mode, auto-resume cancellato)")
        return {"enabled": False}

    @app.post("/admin/clear-cache")
    async def admin_clear_cache():
        n = cache.clear()
        await cache.persist_now()
        log.info("cache cleared (%d entries)", n)
        return {"cleared_entries": n}

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

    @app.get("/get-water-pressure")
    async def get_water_pressure():
        value = await _cached("water_pressure", settings.cache_ttl_water_pressure, client.get_water_pressure)
        return {"pressure": value}

    @app.get("/get-system-info")
    async def get_system_info():
        return await _cached("system_info", settings.cache_ttl_system_info, client.get_system_info)

    return app


INDEX_HTML = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vaillant Smart Server</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; background: #fafafa; }
  h1 { margin-bottom: .2em; }
  h1 small { color: #888; font-size: .55em; font-weight: normal; }
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

<h1>Vaillant Smart Server <small>v0.2.0</small></h1>

<div id="quota-banner" class="quota-banner">
  &#9888; Vaillant ha esaurito la quota API. Auto-resume tra <span id="quota-time">--:--:--</span>.
  L'upstream e' stato messo OFF automaticamente; al replenish il server ri-abilita da solo.
  Manualmente puoi forzare l'abilitazione (Enable) o tenerla OFF (Disable) — entrambi annullano il timer auto-resume.
</div>

<div class="panel">
  <h2>Stato</h2>
  <p>
    Master upstream:
    <span id="master-state" class="pill unknown">caricamento...</span>
    <button id="btn-enable" class="primary" onclick="enableMaster()">Enable</button>
    <button id="btn-disable" class="danger" onclick="disableMaster()">Disable</button>
  </p>
  <p><small class="muted">Quando OFF, il server serve solo dalla cache (anche scaduta) e <strong>non chiama Vaillant</strong>.
    Usalo per liberare l'API mentre usi l'app myVaillant ufficiale o quando hai sforato la quota.</small></p>
</div>

<div class="panel">
  <h2>Zone</h2>
  <button onclick="refreshZones()">&#8635; Ricarica</button>
  <div id="zones-list"><em>caricamento...</em></div>
  <p><small class="muted">Questi sono i valori esatti che il server ritorna. Se OH mostra qualcosa di diverso, e' il binding HTTP che non ha ancora finito un refresh — entro 30s sara' allineato.</small></p>
</div>

<div class="panel">
  <h2>Cache</h2>
  <button onclick="refreshCache()">&#8635; Ricarica</button>
  <button class="danger" onclick="clearCache()">&#128465; Svuota cache</button>
  <table id="cache-table">
    <thead><tr><th>Chiave</th><th>Eta'</th><th>TTL</th><th>Stato</th></tr></thead>
    <tbody><tr><td colspan="4"><em>caricamento...</em></td></tr></tbody>
  </table>
  <p><small class="muted">Le entry "scadute" sono ancora servite (serve-stale) ma al prossimo accesso il server tenta un fetch fresh da Vaillant.</small></p>
</div>

<div class="panel">
  <h2>Probe endpoint</h2>
  <p>
    <button onclick="probe('/zone-info/0')">/zone-info/0</button>
    <button onclick="probe('/zone-info/1')">/zone-info/1</button>
    <button onclick="probe('/zone-info/2')">/zone-info/2</button>
    <button onclick="probe('/get-water-pressure')">/get-water-pressure</button>
    <button onclick="probe('/boiler-consumption-current-month')">/boiler-consumption-current-month</button>
    <button onclick="probe('/boiler-consumption-current-year')">/boiler-consumption-current-year</button>
    <button onclick="probe('/get-system-info')">/get-system-info</button>
  </p>
  <pre id="probe-out">(clicca un endpoint per vederne la risposta)</pre>
</div>

<script>
function formatHMS(totalSec) {
  totalSec = Math.max(0, Math.floor(totalSec));
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
}

async function refreshStatus() {
  try {
    const r = await fetch('/healthz');
    const d = await r.json();
    const el = document.getElementById('master-state');
    if (d.enabled) { el.textContent = 'ON';  el.className = 'pill on'; }
    else            { el.textContent = 'OFF'; el.className = 'pill off'; }

    // Aggiorna il banner auto-resume
    if (typeof d.quota_resume_in_seconds === 'number' && d.quota_resume_in_seconds > 0) {
      showQuotaBanner(formatHMS(d.quota_resume_in_seconds));
    } else {
      hideQuotaBanner();
    }
  } catch (e) {
    document.getElementById('master-state').textContent = 'unreachable';
  }
}
async function enableMaster()  { await fetch('/admin/enable',  { method: 'POST' }); refreshStatus(); }
async function disableMaster() { await fetch('/admin/disable', { method: 'POST' }); refreshStatus(); }

async function refreshCache() {
  try {
    const r = await fetch('/admin/cache');
    const d = await r.json();
    const tbody = document.querySelector('#cache-table tbody');
    const keys = Object.keys(d).sort();
    if (keys.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4"><em>cache vuota</em></td></tr>';
      return;
    }
    tbody.innerHTML = keys.map(k => {
      const v = d[k];
      const cls = v.expired ? 'expired' : 'fresh';
      const txt = v.expired ? 'scaduta' : 'fresca';
      return '<tr>' +
        '<td><code>' + k + '</code></td>' +
        '<td>' + Math.round(v.age_seconds) + 's</td>' +
        '<td>' + v.ttl_seconds + 's</td>' +
        '<td><span class="pill ' + cls + '">' + txt + '</span></td>' +
      '</tr>';
    }).join('');
  } catch (e) {
    document.querySelector('#cache-table tbody').innerHTML =
      '<tr><td colspan="4">errore: ' + e.message + '</td></tr>';
  }
}

async function clearCache() {
  if (!confirm('Svuotare TUTTA la cache? Le prossime chiamate andranno fresh a Vaillant.')) return;
  await fetch('/admin/clear-cache', { method: 'POST' });
  refreshCache();
}

function showQuotaBanner(replenishIn) {
  const el = document.getElementById('quota-banner');
  document.getElementById('quota-time').textContent = replenishIn || '--:--:--';
  el.classList.add('show');
}
function hideQuotaBanner() {
  document.getElementById('quota-banner').classList.remove('show');
}

async function handleResponse(r) {
  // restituisce {ok, status, body, quotaReplenish}
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
  return { ok: r.ok, status: r.status, body, quotaReplenish };
}

const MODE_PILL = { OFF: 'off', MANUAL: 'on', TIME_CONTROLLED: 'fresh' };
const MODE_LABEL = { off: 'Off', manual: 'Manuale', time_controlled: 'Programma' };

async function refreshZones() {
  const out = document.getElementById('zones-list');
  try {
    const zsR = await fetch('/zones');
    const zs = await zsR.json();
    if (!Array.isArray(zs)) {
      out.textContent = 'risposta inattesa da /zones: ' + JSON.stringify(zs);
      return;
    }
    const infos = await Promise.all(zs.map(z =>
      fetch('/zone-info/' + z.index).then(r => r.json())
    ));
    out.innerHTML = zs.map((z, i) => renderZone(z, infos[i])).join('');
  } catch (e) {
    out.textContent = 'errore: ' + e.message;
  }
}

function renderZone(z, info) {
  const idx = z.index;
  const mode = (info && info.heating_state) || '?';
  const pillCls = MODE_PILL[mode] || 'unknown';
  const curT = info && typeof info.current_temperature === 'number'
    ? info.current_temperature.toFixed(2) + ' °C' : '?';
  const spT  = info && typeof info.desired_temperature === 'number'
    ? info.desired_temperature.toFixed(1) + ' °C' : '?';
  const name = z.name || ('Zone ' + idx);
  const spVal = (info && typeof info.desired_temperature === 'number') ? info.desired_temperature : 20;
  // Template literals (backticks) per evitare guai di escaping con apostrofi italiani.
  return `
    <div class="zone-card" id="zone-${idx}">
      <h3>${name} <small class="muted">[idx=${idx}]</small></h3>
      <div class="zone-row">
        <div><span class="label">Temperatura</span><span class="val">${curT}</span></div>
        <div><span class="label">Setpoint</span><span class="val">${spT}</span></div>
        <div><span class="label">Modalita</span><span class="pill ${pillCls}">${mode}</span></div>
      </div>
      <div class="zone-actions">
        Modalita:
        <button onclick="setZoneMode(${idx}, 'off')">Off</button>
        <button onclick="setZoneMode(${idx}, 'manual')">Manuale</button>
        <button onclick="setZoneMode(${idx}, 'time_controlled')">Programma</button>
      </div>
      <div class="zone-actions">
        Setpoint:
        <input type="number" id="sp-${idx}" value="${spVal}" step="0.5" min="5" max="30">&deg;C
        <button onclick="setZoneSetpoint(${idx})">Imposta</button>
        <small class="muted">(richiede modalita Manuale)</small>
      </div>
    </div>
  `;
}

async function setZoneMode(idx, mode) {
  const r = await fetch('/zone-update/' + idx + '/' + mode);
  const res = await handleResponse(r);
  if (!res.ok) {
    alert('Errore ' + res.status + ': ' + JSON.stringify(res.body));
    return;
  }
  setTimeout(refreshZones, 500);
  setTimeout(refreshCache, 800);
}

async function setZoneSetpoint(idx) {
  const val = document.getElementById('sp-' + idx).value;
  if (!val || isNaN(parseFloat(val))) {
    alert('setpoint non valido: ' + val);
    return;
  }
  const r = await fetch('/zone-set-temp/' + idx + '/' + val);
  const res = await handleResponse(r);
  if (!res.ok) {
    alert('Errore ' + res.status + ': ' + JSON.stringify(res.body));
    return;
  }
  setTimeout(refreshZones, 500);
  setTimeout(refreshCache, 800);
}

async function probe(path) {
  const out = document.getElementById('probe-out');
  out.textContent = 'GET ' + path + '\\n...';
  const t0 = performance.now();
  try {
    const r = await fetch(path);
    const dt = Math.round(performance.now() - t0);
    const text = await r.text();
    let pretty;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); }
    catch (e) { pretty = text || '(body vuoto)'; }

    let header = 'GET ' + path + '\\nHTTP ' + r.status + '  in ' + dt + 'ms';
    if (r.status === 429) {
      try {
        const d = JSON.parse(text);
        const replenish = d.detail && d.detail.replenish_in;
        if (replenish) {
          showQuotaBanner(replenish);
          header += '\\n\\n\\u26A0 VAILLANT QUOTA ESAURITA. Replenish in ' + replenish;
        }
      } catch (e) {}
    } else if (r.status === 200 && path.startsWith('/zone-info')) {
      hideQuotaBanner();
    }

    out.textContent = header + '\\n\\n' + pretty;
    refreshCache();
  } catch (e) {
    out.textContent = 'errore: ' + e.message;
  }
}

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
