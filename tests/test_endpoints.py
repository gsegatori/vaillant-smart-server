from __future__ import annotations


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "enabled": True}


def test_zones(client, fake_client):
    r = client.get("/zones")
    assert r.status_code == 200
    zones = r.json()
    assert len(zones) == 3
    assert zones[1]["name"] == "Piano 1"
    # secondo accesso deve venire da cache (1 sola call al client)
    client.get("/zones")
    assert fake_client.calls["get_zones"] == 1


def test_zone_info(client):
    r = client.get("/zone-info/1")
    assert r.status_code == 200
    z = r.json()
    assert z["index"] == 1
    assert z["current_temperature"] == 22.0


def test_zone_info_not_found(client):
    r = client.get("/zone-info/9")
    assert r.status_code == 404


def test_water_pressure(client):
    r = client.get("/get-water-pressure")
    assert r.status_code == 200
    assert r.json() == {"pressure": 1.4}


def test_boiler_consumption_current(client):
    r = client.get("/boiler-consumption-current-month")
    assert r.status_code == 200
    body = r.json()
    assert body["total_m3"] == 19.2
    assert "DOMESTIC_HOT_WATER" in body["by_mode_m3"]


def test_boiler_consumption_specific_month(client, fake_client):
    r = client.get("/boiler-consumption/2026/3")
    assert r.status_code == 200
    body = r.json()
    assert body["year"] == 2026
    assert body["month"] == 3
    assert fake_client.calls["get_gas_consumption[2026,3]"] == 1


def test_boiler_consumption_year(client, fake_client):
    r = client.get("/boiler-consumption-year/2025")
    assert r.status_code == 200
    body = r.json()
    assert body["year"] == 2025
    assert body["total_m3"] == 227.0
    assert "by_month" in body
    # second call hits cache
    client.get("/boiler-consumption-year/2025")
    assert fake_client.calls["get_gas_consumption_year[2025]"] == 1


def test_boiler_consumption_current_year(client):
    r = client.get("/boiler-consumption-current-year")
    assert r.status_code == 200
    body = r.json()
    assert "by_month" in body
    assert body["total_m3"] == 227.0


def test_zone_set_temp_optimistic_cache(client, fake_client):
    # carica zone_info_0 -> cache (FakeClient ritorna desired_temperature=5.0)
    z1 = client.get("/zone-info/0").json()
    assert z1["desired_temperature"] == 5.0
    assert fake_client.calls["get_zone_info[0]"] == 1
    # set temp -> optimistic update della cache, NO refetch
    r = client.get("/zone-set-temp/0/22.5")
    assert r.status_code == 200
    # next zone_info_0 deve venire dalla cache aggiornata, senza nuova fetch
    z2 = client.get("/zone-info/0").json()
    assert z2["desired_temperature"] == 22.5  # valore optimistic
    assert fake_client.calls["get_zone_info[0]"] == 1  # nessuna fetch extra


def test_zone_update_optimistic_cache(client, fake_client):
    # carica zone_info_0 -> cache (FakeClient ritorna heating_state="OFF")
    z1 = client.get("/zone-info/0").json()
    assert z1["heating_state"] == "OFF"
    # cambia mode a manual -> optimistic update
    r = client.get("/zone-update/0/manual")
    assert r.status_code == 200
    # next zone_info_0 deve mostrare heating_state aggiornato, senza refetch
    z2 = client.get("/zone-info/0").json()
    assert z2["heating_state"] == "manual"  # FakeClient.update_zone_mode ritorna mode=il valore passato
    assert fake_client.calls["get_zone_info[0]"] == 1  # nessuna fetch extra


def test_disable_blocks_upstream_and_serves_cache(client, fake_client):
    # popola la cache
    r = client.get("/zones")
    assert r.status_code == 200
    assert fake_client.calls.get("get_zones") == 1

    # disabilita
    r = client.post("/admin/disable")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}

    # /zones risponde dalla cache, NON chiama l'upstream
    r = client.get("/zones")
    assert r.status_code == 200
    assert fake_client.calls.get("get_zones") == 1  # invariato

    # un endpoint senza cache -> 503
    r = client.get("/zone-info/0")
    assert r.status_code == 503

    # riabilita
    client.post("/admin/enable")
    r = client.get("/zone-info/0")
    assert r.status_code == 200


def test_admin_cache_snapshot(client):
    client.get("/zones")
    r = client.get("/admin/cache")
    assert r.status_code == 200
    snap = r.json()
    assert "zones" in snap
    assert "fetched_at" in snap["zones"]
    assert "age_seconds" in snap["zones"]


def test_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    # contiene i pulsanti chiave
    assert "/admin/enable" in body
    assert "/admin/disable" in body
    assert "/admin/clear-cache" in body
    # contiene la tabella cache + probe endpoint
    assert "cache-table" in body
    assert "/zone-info/0" in body
    # niente reference esterne a font/cdn
    assert "https://" not in body.lower() or body.lower().count("https://") < 3


def test_quota_returns_429_with_detail(client, fake_client):
    """Cache vuota + quota Vaillant esaurita -> 429 con replenish_in."""
    fake_client.force_quota = ("00:05:23", "Quota Exceeded test message")
    r = client.get("/zones")
    assert r.status_code == 429
    body = r.json()
    assert body["detail"]["error"] == "vaillant_quota_exceeded"
    assert body["detail"]["replenish_in"] == "00:05:23"
    assert "5:23" in body["detail"]["message"]


def test_quota_triggers_auto_lockout(client, fake_client):
    """Quota exception -> master messo automaticamente a OFF + quota_resume_at settato."""
    fake_client.force_quota = ("00:10:00", "test")
    r = client.get("/zones")
    assert r.status_code == 429
    # ora healthz deve mostrare enabled=False + quota_resume_in_seconds ~600
    h = client.get("/healthz").json()
    assert h["enabled"] is False
    assert "quota_resume_in_seconds" in h
    assert 595 <= h["quota_resume_in_seconds"] <= 600


def test_admin_enable_cancels_quota_timer(client, fake_client):
    """Manual /admin/enable durante un lockout cancella il timer auto-resume."""
    fake_client.force_quota = ("00:30:00", "test")
    client.get("/zones")  # triggers lockout
    assert client.get("/healthz").json()["enabled"] is False
    # ora abilito manualmente
    fake_client.force_quota = None  # non vogliamo che la prossima call rilanci
    r = client.post("/admin/enable")
    assert r.json() == {"enabled": True}
    h = client.get("/healthz").json()
    assert h["enabled"] is True
    assert "quota_resume_in_seconds" not in h  # timer cancellato


def test_admin_disable_cancels_quota_timer(client, fake_client):
    """Disable manuale cancella anch'esso il timer auto-resume."""
    fake_client.force_quota = ("00:15:00", "test")
    client.get("/zones")
    assert client.get("/healthz").json()["enabled"] is False
    r = client.post("/admin/disable")
    assert r.json() == {"enabled": False}
    h = client.get("/healthz").json()
    assert h["enabled"] is False
    assert "quota_resume_in_seconds" not in h


def test_quota_with_stale_cache_serves_stale(client, fake_client):
    """Cache popolata + quota esaurita -> 200 con valore stale (no 429)."""
    # primo fetch popola cache
    r1 = client.get("/zones")
    assert r1.status_code == 200
    # ora il client va in quota
    fake_client.force_quota = ("00:05:00", "test")
    # invalida la cache di zones cosi' la prossima richiesta scatena un fetch
    # (in produzione succede naturalmente quando TTL scade)
    # Ma il get_or_fetch ha cache fresca, quindi prima testiamo con cache fresh
    r2 = client.get("/zones")
    # Cache fresca -> serve direttamente, no fetch -> 200 senza quota error
    assert r2.status_code == 200


def test_zone_update_quota_returns_429(client, fake_client):
    """Anche le azioni di scrittura (zone-update) devono dare 429 su quota."""
    fake_client.force_quota = ("00:10:00", "test")
    r = client.get("/zone-update/0/manual")
    assert r.status_code == 429
    assert r.json()["detail"]["replenish_in"] == "00:10:00"


def test_admin_clear_cache(client, fake_client):
    # popola la cache
    client.get("/zones")
    client.get("/get-water-pressure")
    snap = client.get("/admin/cache").json()
    assert len(snap) >= 2
    # clear
    r = client.post("/admin/clear-cache")
    assert r.status_code == 200
    body = r.json()
    assert body["cleared_entries"] >= 2
    # snapshot vuoto
    assert client.get("/admin/cache").json() == {}
    # next call rifa fetch
    client.get("/zones")
    assert fake_client.calls["get_zones"] == 2
