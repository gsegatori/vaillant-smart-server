# vaillant-smart-server

FastAPI server attorno a [myPyllant](https://github.com/signalkraft/myPyllant). Esposto a OpenHAB per leggere consumo gas, temperature zone, pressione acqua di un sistema Vaillant ibrido (heatpump + boiler) e per impostare modalita'/setpoint.

Caratteristiche chiave per **non farsi rate-limitare da Vaillant**:

- **Cache persistente su disco** (`/data/cache.json`) — sopravvive ai restart, evita di bombardare l'API al boot.
- **TTL diverso per ogni tipo di dato**: gas 4h (cambia mensile), zone 5 min, water pressure 10 min, ecc.
- **Single-flight** per chiave: due richieste concorrenti scatenano una sola chiamata upstream.
- **Serve-stale-on-error**: se l'API Vaillant ti banna o e' giu', gli endpoint ritornano l'ultimo valore noto invece di errore.
- **Retry exp-backoff** sui transient (timeout, ClientError, ConnectionError) — default 3 tentativi con `2^attempt` secondi di pausa.
- **Master kill-switch** (`POST /admin/disable` / `POST /admin/enable`): quando disabilitato, gli endpoint servono solo cache (anche scaduta) e **non chiamano Vaillant** — utile per liberare l'API mentre usi l'app ufficiale myVaillant.

## Endpoint

### Compat OpenHAB (URL legacy del progetto v1)

| Verbo | Path | Cache TTL | Output |
|---|---|---|---|
| GET | `/boiler-consumption/{year}/{month}` | 4h | breakdown gas per `DOMESTIC_HOT_WATER`/`HEATING` + totale m³ |
| GET | `/boiler-consumption-current-month` | 4h | uguale al precedente, mese corrente |
| GET | `/zones` | 30min | lista zone (index, nome) |
| GET | `/zone-info/{idx}` | 5min | temp corrente, setpoint, heating state |
| GET | `/zone-flow-temp/{idx}` | 5min | temperatura mandata del circuito |
| GET | `/zone-update/{idx}/{mode}` | — | imposta `manual`/`off`/`time_controlled`, invalida cache |
| GET | `/zone-set-temp/{idx}/{temp}` | — | imposta setpoint, invalida cache |
| GET | `/get-water-pressure` | 10min | pressione bar |
| GET | `/get-system-info` | 5min | dump intero system serializzato |

### Infra

| Verbo | Path | Cosa fa |
|---|---|---|
| GET  | `/healthz` | liveness probe, no chiamate Vaillant |
| GET  | `/admin/cache` | snapshot stato cache (chiavi, eta', se scaduto) |
| POST | `/admin/enable` | abilita chiamate upstream |
| POST | `/admin/disable` | disabilita upstream, serve solo cache |

## Setup

### Locale (dev)

```bash
cp .env.example .env
# imposta VAILLANT_USER + VAILLANT_PASSWORD nel .env
python3.12 -m venv .venv && .venv/bin/pip install -e ".[test]"
CACHE_FILE=/tmp/vaillant-cache.json BIND_PORT=5001 .venv/bin/python -m app.main
curl http://localhost:5001/zones
```

### Docker (deploy mini PC)

```bash
git clone git@github.com:gsegatori/vaillant-smart-server.git ~/vaillant-smart-server
cd ~/vaillant-smart-server
./update.sh
nano .env  # VAILLANT_USER, VAILLANT_PASSWORD
docker compose restart vaillant-smart-server
```

Aggiornamenti successivi (Plex-style): `cd ~/vaillant-smart-server && ./update.sh`.

⚠ Se modifichi solo `.env` (senza `git pull`), **NON usare** `docker compose restart` — Docker Compose non rilegge il file env su restart. Usa `docker compose up -d --force-recreate` (oppure `./update.sh` che fa già `down + up -d --build`).

## Tuning TTL

Vaillant non documenta i rate limit, ma spammando ti banna per ~ore. I default sono conservativi. Se vuoi cambiarli, edita le variabili `CACHE_TTL_*` nel `.env` (in secondi). Esempio: gas consumption a 24h se il grafico mensile basta aggiornato 1×/giorno → `CACHE_TTL_GAS=86400`.

## Kill-switch dal sitemap OpenHAB

Pattern (come per Rainbird):
1. Crea item `Vaillant_Master_Enabled` (Switch) in OH.
2. Rule che su `received command`:
   - ON → `executeCommandLine ... curl -X POST http://<host>:5000/admin/enable`
   - OFF → idem con `/admin/disable`
3. Sitemap nasconde tutto il resto se `Vaillant_Master_Enabled != ON`.

Cosi' col tap di un bottone "blocca tutto" liberi Vaillant per usare l'app ufficiale.

## Struttura

```
app/
├── __init__.py
├── config.py         pydantic-settings, lookup .env in piu' posti
├── cache.py          PersistentCache JSON con TTL/serve-stale/single-flight
├── client.py         VaillantClient wrapper myPyllant + retry/backoff
└── main.py           FastAPI app + endpoints + middleware access log

tests/                17 test unitari (cache + endpoint con FakeClient mocked)
Dockerfile            multi-stage non-root su python:3.12-slim
docker-compose.yml    host network, healthcheck su /healthz
update.sh             deploy/auto-update helper (clone -> pull -> rebuild)
```

## Test

```bash
.venv/bin/pytest                # 17 test verdi
```

Niente test che richiede credenziali Vaillant reali; tutti via `FakeVaillantClient` in `tests/conftest.py`. Lo smoke contro Vaillant reale e' a discrezione (`python -m app.main` + curl).

## Storico v1 (legacy)

La v1 era una Flask app con cache solo in-memory (volatile) e nessun serve-stale. Sostituita da questa v2 con FastAPI + cache persistente + kill-switch. Mantenuti tutti gli URL legacy per non rompere le regole OpenHAB esistenti.
