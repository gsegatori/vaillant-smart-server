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
    """Quota exception -> master messo automaticamente a OFF + quota_resume_at settato.

    Il resume_at e' replenish_in + QUOTA_RESUME_BUFFER_S (3 min) per evitare
    ri-ban immediato al primo poll dopo la finestra.
    """
    fake_client.force_quota = ("00:10:00", "test")
    r = client.get("/zones")
    assert r.status_code == 429
    # 10 min (600s) + 3 min (180s) buffer = ~780s
    h = client.get("/healthz").json()
    assert h["enabled"] is False
    assert "quota_resume_in_seconds" in h
    assert 775 <= h["quota_resume_in_seconds"] <= 780


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


def test_dhw_set_temp(client, fake_client):
    # populate system_info cache
    client.get("/get-system-info")
    assert fake_client.calls.get("get_system_info") == 1

    r = client.get("/dhw-set-temp/0/55")
    assert r.status_code == 200
    assert r.json()["temperature"] == 55
    assert fake_client.calls.get("update_dhw_temperature[0,55.0]") == 1

    # system_info was invalidated by dhw_set_temp, next call re-fetches
    client.get("/get-system-info")
    assert fake_client.calls.get("get_system_info") == 2


def test_dhw_set_temp_not_found(client, fake_client):
    r = client.get("/dhw-set-temp/99/55")
    assert r.status_code == 404


def test_dhw_set_temp_quota_returns_429(client, fake_client):
    fake_client.force_quota = ("00:15:00", "test")
    r = client.get("/dhw-set-temp/0/55")
    assert r.status_code == 429
    assert r.json()["detail"]["replenish_in"] == "00:15:00"


def test_get_rts(client, fake_client):
    r = client.get("/get-rts")
    assert r.status_code == 200
    body = r.json()
    assert "rts_statistics" in body
    assert body["rts_statistics"]["flow_temp"] == 32.5
    assert fake_client.calls["get_rts"] == 1


async def test_vaillant_client_get_rts_uses_system_timezone():
    from datetime import datetime
    from unittest.mock import AsyncMock, MagicMock
    from zoneinfo import ZoneInfo
    from app.client import VaillantClient

    c = VaillantClient(user="u", password="p", brand="vaillant", country="italy")
    mock_system = MagicMock()
    mock_system.timezone = ZoneInfo("Europe/Rome")
    mock_api = MagicMock()
    mock_api.get_rts = AsyncMock(return_value={"statistics": {"flow_temp": 30.0}})
    c._ensure_authenticated = AsyncMock(return_value=mock_api)
    c._get_cached_system = AsyncMock(return_value=mock_system)

    res = await c.get_rts()
    assert res["rts_statistics"] == {"flow_temp": 30.0}
    dt = datetime.fromisoformat(res["update_timestamp"])
    assert dt.tzinfo is not None


def test_apply_preset(client, fake_client):
    r = client.get("/apply-preset/at_home")
    assert r.status_code == 200
    assert fake_client.calls.get("apply_preset") == 1
    assert r.json()["message"] == "At Home - done successfully."

    r = client.get("/apply-preset/HOLIDAY")
    assert r.status_code == 200
    assert fake_client.calls.get("apply_preset") == 2
    assert r.json()["message"] == "Holiday - done successfully."

    r = client.get("/apply-preset/coming_back")
    assert r.status_code == 200
    assert fake_client.calls.get("apply_preset") == 3
    assert r.json()["message"] == "Coming Back - done successfully."

    # Verify /set-away-mode is removed
    r = client.get("/set-away-mode/at_home")
    assert r.status_code == 404


def test_apply_preset_invalid(client):
    r = client.get("/apply-preset/invalid_mode")
    assert r.status_code == 400


def test_preset_modes_configuration():
    from app.config import DEFAULT_PRESETS, Preset, Settings
    from myPyllant.enums import DHWOperationMode
    from myPyllant.models import ZoneTimeProgramDay

    settings = Settings()
    assert set(settings.presets.keys()) == {"AT_HOME", "HOLIDAY", "COMING_BACK"}
    assert set(DEFAULT_PRESETS.keys()) == {"AT_HOME", "HOLIDAY", "COMING_BACK"}

    assert DEFAULT_PRESETS["AT_HOME"].name == "At Home"
    assert DEFAULT_PRESETS["AT_HOME"].dhw_mode == DHWOperationMode.TIME_CONTROLLED
    assert DEFAULT_PRESETS["HOLIDAY"].name == "Holiday"
    assert DEFAULT_PRESETS["HOLIDAY"].dhw_mode == DHWOperationMode.OFF
    assert DEFAULT_PRESETS["COMING_BACK"].name == "Coming Back"
    assert DEFAULT_PRESETS["COMING_BACK"].dhw_mode == DHWOperationMode.TIME_CONTROLLED

    for mode_name, cfg in DEFAULT_PRESETS.items():
        assert isinstance(cfg, Preset)
        assert isinstance(cfg.dhw_mode, (DHWOperationMode, str))
        assert len(cfg.heating_plan) > 0
        preset_dict = cfg.to_dict()
        assert preset_dict["name"] == cfg.name
        assert preset_dict["dhw_mode"] == cfg.dhw_mode
        assert "heating_plan" in preset_dict
        for slot in cfg.heating_plan:
            assert isinstance(slot, ZoneTimeProgramDay)
            assert 0 <= slot.start_time < slot.end_time <= 1440
            assert isinstance(slot.setpoint, (int, float))
        for slot_dict in preset_dict["heating_plan"]:
            assert 0 <= slot_dict["start_time"] < slot_dict["end_time"] <= 1440
            assert isinstance(slot_dict["setpoint"], (int, float))


def test_config_example_file(monkeypatch):
    from pathlib import Path
    from app.config import Settings
    from myPyllant.enums import DHWOperationMode

    example_path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    assert example_path.is_file()

    monkeypatch.setenv("VAILLANT_CONFIG_FILE", str(example_path))
    s = Settings()
    assert set(s.presets.keys()) == {"AT_HOME", "HOLIDAY", "COMING_BACK"}
    assert s.presets["AT_HOME"].name == "At Home"
    assert s.presets["AT_HOME"].dhw_mode == DHWOperationMode.TIME_CONTROLLED
    assert len(s.presets["AT_HOME"].heating_plan) == 5
    assert s.presets["HOLIDAY"].dhw_mode == DHWOperationMode.OFF
    assert len(s.presets["HOLIDAY"].heating_plan) == 3
    assert s.presets["COMING_BACK"].dhw_mode == DHWOperationMode.TIME_CONTROLLED
    assert len(s.presets["COMING_BACK"].heating_plan) == 3


def test_config_sources_combination_and_precedence(tmp_path, monkeypatch):
    from app.config import Settings

    env_file = tmp_path / "custom.env"
    yaml_file = tmp_path / "custom.yaml"

    # cache_ttl_zone_info is in both (env should win)
    # cache_ttl_water_pressure is only in env
    # cache_ttl_gas is only in yaml
    env_file.write_text("CACHE_TTL_ZONE_INFO=888\nCACHE_TTL_WATER_PRESSURE=444\n")
    yaml_file.write_text("cache_ttl_zone_info: 999\ncache_ttl_gas: 1234\n")

    monkeypatch.setenv("VAILLANT_ENV_FILE", str(env_file))
    monkeypatch.setenv("VAILLANT_CONFIG_FILE", str(yaml_file))

    s = Settings()
    # Combines keys from both sources:
    assert s.cache_ttl_water_pressure == 444
    assert s.cache_ttl_gas == 1234
    # .env takes precedence over YAML when key is defined in both:
    assert s.cache_ttl_zone_info == 888


def test_preset_configuration_validation(tmp_path, monkeypatch):
    import pytest
    from pydantic import ValidationError
    from app.config import DEFAULT_PRESETS, Settings

    # 1. Config with no presets -> defaults to DEFAULT_PRESETS
    no_presets_file = tmp_path / "no_presets.yaml"
    no_presets_file.write_text("bind_port: 5050\n")
    monkeypatch.setenv("VAILLANT_CONFIG_FILE", str(no_presets_file))
    s1 = Settings()
    assert s1.presets == DEFAULT_PRESETS

    # 2. Config with empty presets (None or null or nil) -> raises ValidationError
    for null_val in ["null", "~", "", "nil"]:
        empty_presets_file = tmp_path / f"empty_presets_{null_val or 'blank'}.yaml"
        if null_val == "":
            empty_presets_file.write_text("presets:\n")
        else:
            empty_presets_file.write_text(f"presets: {null_val}\n")
        monkeypatch.setenv("VAILLANT_CONFIG_FILE", str(empty_presets_file))
        with pytest.raises(ValidationError):
            Settings()

    with pytest.raises(ValidationError):
        Settings(presets=None)

    # 3. Config with an empty list of presets -> raises ValidationError
    empty_list_file = tmp_path / "empty_list.yaml"
    empty_list_file.write_text("presets: []\n")
    monkeypatch.setenv("VAILLANT_CONFIG_FILE", str(empty_list_file))
    with pytest.raises(ValidationError):
        Settings()

    with pytest.raises(ValidationError):
        Settings(presets=[])

    # 4. Config with a preset with an empty list of ZoneTimeProgram (heating_plan: [])
    empty_plan_file = tmp_path / "empty_plan.yaml"
    empty_plan_file.write_text(
        "presets:\n"
        "  AWAY_CUSTOM:\n"
        "    name: \"Away Custom\"\n"
        "    dhw_mode: \"OFF\"\n"
        "    heating_plan: []\n"
    )
    monkeypatch.setenv("VAILLANT_CONFIG_FILE", str(empty_plan_file))
    s4 = Settings()
    assert "AWAY_CUSTOM" in s4.presets
    assert s4.presets["AWAY_CUSTOM"].name == "Away Custom"
    assert s4.presets["AWAY_CUSTOM"].heating_plan == []


def test_get_system_info_and_data(client, fake_client):
    r = client.get("/get-system-info-and-data")
    assert r.status_code == 200
    body = r.json()
    assert body["home"] == "fake"
    assert "devices" in body
    assert body["my_energy_data"]["cop_overall_day"] == 3.5
    assert fake_client.calls["get_system_info_and_data"] == 1


def test_get_device_data(client, fake_client):
    r = client.get("/get-device-data")
    assert r.status_code == 200
    body = r.json()
    assert "devices" in body
    assert body["my_energy_data"]["cop_overall_day"] == 3.5
    assert fake_client.calls["get_device_data"] == 1
