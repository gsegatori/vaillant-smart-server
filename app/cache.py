"""Cache persistente JSON con TTL, serve-stale-on-error e single-flight per chiave.

Pattern:
- get_or_fetch(key, ttl, fetcher) ritorna value e serve-stale-flag.
- Se l'entry esiste e non e' scaduta: ritorna subito.
- Altrimenti acquisisce un lock per quella chiave (single-flight) e chiama fetcher.
- Se fetcher fallisce ma c'e' un valore vecchio in cache: lo ritorna marcato stale.
- Persistenza atomica via tempfile + rename.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class _Entry:
    value: Any
    fetched_at: float  # unix timestamp
    ttl_seconds: int


class PersistentCache:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._data: dict[str, _Entry] = {}
        self._write_lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not self._path.exists():
            log.info("cache file %s missing, starting empty", self._path)
            return
        try:
            raw = json.loads(self._path.read_text())
            for k, v in raw.items():
                self._data[k] = _Entry(value=v["value"], fetched_at=v["fetched_at"], ttl_seconds=v["ttl_seconds"])
            log.info("cache loaded %d entries from %s", len(self._data), self._path)
        except (OSError, ValueError, KeyError) as e:
            log.warning("cache file %s unreadable (%s), starting empty", self._path, e)

    def _key_lock(self, key: str) -> asyncio.Lock:
        if key not in self._key_locks:
            self._key_locks[key] = asyncio.Lock()
        return self._key_locks[key]

    def _is_fresh(self, entry: _Entry, now: float) -> bool:
        return (now - entry.fetched_at) < entry.ttl_seconds

    async def get_or_fetch(
        self,
        key: str,
        ttl_seconds: int,
        fetcher: Callable[[], Awaitable[Any]],
        force_refresh: bool = False,
    ) -> tuple[Any, bool]:
        """Returns (value, is_stale). Raises only if fetch fails and no cache available."""
        now = time.time()
        cached = self._data.get(key)
        if cached and not force_refresh and self._is_fresh(cached, now):
            return cached.value, False

        async with self._key_lock(key):
            # re-check inside lock (another task may have refreshed)
            now = time.time()
            cached = self._data.get(key)
            if cached and not force_refresh and self._is_fresh(cached, now):
                return cached.value, False

            try:
                value = await fetcher()
                self._data[key] = _Entry(value=value, fetched_at=now, ttl_seconds=ttl_seconds)
                await self._persist()
                return value, False
            except Exception as e:
                if cached is not None:
                    age_s = now - cached.fetched_at
                    log.warning(
                        "fetch '%s' failed (%s); serving stale (age=%.0fs)",
                        key, e, age_s,
                    )
                    return cached.value, True
                raise

    async def serve_only(self, key: str) -> Any | None:
        """Ritorna SEMPRE quello che e' in cache anche se scaduto, senza chiamare il fetcher.

        Usato quando il sistema e' in modalita' 'disabled' (kill-switch attivo).
        """
        entry = self._data.get(key)
        return entry.value if entry else None

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    async def persist_now(self) -> None:
        await self._persist()

    async def _persist(self) -> None:
        async with self._write_lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                k: {"value": e.value, "fetched_at": e.fetched_at, "ttl_seconds": e.ttl_seconds}
                for k, e in self._data.items()
            }
            await asyncio.to_thread(self._atomic_write, payload)

    def _atomic_write(self, payload: dict) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".cache.", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def snapshot(self) -> dict[str, dict]:
        """Per /admin/cache: tutto lo stato corrente con eta' relativa."""
        now = time.time()
        out: dict[str, dict] = {}
        for k, e in self._data.items():
            age = now - e.fetched_at
            out[k] = {
                "value": e.value,
                "fetched_at": e.fetched_at,
                "age_seconds": round(age, 1),
                "ttl_seconds": e.ttl_seconds,
                "expired": age >= e.ttl_seconds,
            }
        return out
