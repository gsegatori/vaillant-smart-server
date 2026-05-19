from __future__ import annotations

import asyncio

import pytest

from app.cache import PersistentCache


pytestmark = pytest.mark.asyncio


async def test_fresh_hit_no_fetch(tmp_path):
    c = PersistentCache(tmp_path / "c.json")
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return {"v": 42}

    v1, stale1 = await c.get_or_fetch("k", 60, fetcher)
    assert v1 == {"v": 42}
    assert not stale1
    v2, stale2 = await c.get_or_fetch("k", 60, fetcher)
    assert v2 == {"v": 42}
    assert not stale2
    assert calls["n"] == 1  # fetcher chiamato solo la prima volta


async def test_expired_triggers_fetch(tmp_path):
    c = PersistentCache(tmp_path / "c.json")
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return calls["n"]

    v1, _ = await c.get_or_fetch("k", 0, fetcher)  # ttl 0 -> expired subito
    await asyncio.sleep(0.01)
    v2, _ = await c.get_or_fetch("k", 0, fetcher)
    assert v1 == 1
    assert v2 == 2
    assert calls["n"] == 2


async def test_serve_stale_on_fetch_failure(tmp_path):
    c = PersistentCache(tmp_path / "c.json")
    calls = {"n": 0}

    async def fetcher_ok():
        calls["n"] += 1
        return "good"

    async def fetcher_fail():
        raise RuntimeError("API giu'")

    # primo fetch riuscito -> cache fresca
    v1, stale1 = await c.get_or_fetch("k", 0, fetcher_ok)
    assert v1 == "good" and not stale1

    # secondo fetch fallisce ma c'e' valore vecchio -> serve stale
    await asyncio.sleep(0.01)
    v2, stale2 = await c.get_or_fetch("k", 0, fetcher_fail)
    assert v2 == "good"
    assert stale2 is True


async def test_failure_with_no_cache_raises(tmp_path):
    c = PersistentCache(tmp_path / "c.json")

    async def fetcher_fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await c.get_or_fetch("k", 60, fetcher_fail)


async def test_persistence_survives_reload(tmp_path):
    path = tmp_path / "c.json"
    c1 = PersistentCache(path)

    async def fetcher():
        return [1, 2, 3]

    await c1.get_or_fetch("k", 600, fetcher)
    # reload da disco -> stessi dati
    c2 = PersistentCache(path)
    v, stale = await c2.get_or_fetch("k", 600, lambda: (_ for _ in ()).throw(AssertionError("non doveva chiamare il fetcher")))
    assert v == [1, 2, 3]
    assert not stale


async def test_single_flight_serializes_concurrent_fetch(tmp_path):
    """Due richieste concorrenti su cache vuota -> un solo fetch reale (single-flight).

    Usiamo TTL>0 cosi' quando t2 entra nel lock dopo t1, vede l'entry fresca
    e ritorna subito senza chiamare il fetcher.
    """
    c = PersistentCache(tmp_path / "c.json")
    calls = {"n": 0}
    started = asyncio.Event()
    can_finish = asyncio.Event()

    async def slow_fetcher():
        calls["n"] += 1
        started.set()
        await can_finish.wait()
        return calls["n"]

    t1 = asyncio.create_task(c.get_or_fetch("k", 60, slow_fetcher))
    await started.wait()
    t2 = asyncio.create_task(c.get_or_fetch("k", 60, slow_fetcher))
    await asyncio.sleep(0.01)
    assert not t2.done()
    can_finish.set()
    (v1, _), (v2, _) = await asyncio.gather(t1, t2)
    assert calls["n"] == 1
    assert v1 == v2 == 1


async def test_invalidate(tmp_path):
    c = PersistentCache(tmp_path / "c.json")
    calls = {"n": 0}

    async def fetcher():
        calls["n"] += 1
        return calls["n"]

    await c.get_or_fetch("k", 60, fetcher)
    c.invalidate("k")
    await c.get_or_fetch("k", 60, fetcher)
    assert calls["n"] == 2


async def test_serve_only_returns_cached_even_expired(tmp_path):
    c = PersistentCache(tmp_path / "c.json")

    async def fetcher():
        return "value"

    await c.get_or_fetch("k", 0, fetcher)
    assert await c.serve_only("k") == "value"
    assert await c.serve_only("missing") is None
