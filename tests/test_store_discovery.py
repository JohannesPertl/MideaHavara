import json

import pytest

from tracker.store_discovery import Area, _hydration_json, _jsonld_geo, add_area_and_refresh, config_areas, remove_area_and_refresh


def test_hydration_json_decodes_router_data():
    data = {"loaderData": {"0-17": {"data": {"localStorePage": {"uid": "store-finder"}}}}}
    encoded = json.dumps(json.dumps(data))[1:-1]
    html = f'<script>window.__staticRouterHydrationData = JSON.parse("{encoded}");</script>'

    assert _hydration_json(html)["loaderData"]["0-17"]["data"]["localStorePage"]["uid"] == "store-finder"


def test_jsonld_geo_extracts_coordinates():
    html = '"geo":{"@type":"GeoCoordinates","latitude":47.059726,"longitude":15.428332}'

    assert _jsonld_geo(html) == (47.059726, 15.428332)


def test_config_areas_empty_list_disables_legacy_fallback(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "areas: []\nlocation:\n  city: Graz\n  postal_code: '8010'\n  latitude: 47\n  longitude: 15\n  radius_km: 25\n",
        encoding="utf-8",
    )

    assert config_areas(config_path) == []


def test_remove_area_and_refresh(monkeypatch):
    saved = {}
    monkeypatch.setattr("tracker.store_discovery.config_areas", lambda: [Area("Graz", 47, 15, 25), Area("Wien", 48, 16, 40)])
    monkeypatch.setattr("tracker.store_discovery.discover_mediamarkt_stores", lambda areas: [])
    monkeypatch.setattr("tracker.store_discovery.save_areas_and_stores", lambda areas, stores: saved.update({"areas": areas, "stores": stores}))

    removed, stores = remove_area_and_refresh(2)

    assert removed.name == "Wien"
    assert [a.name for a in saved["areas"]] == ["Graz"]
    assert stores == []


def test_remove_area_rejects_bad_index(monkeypatch):
    monkeypatch.setattr("tracker.store_discovery.config_areas", lambda: [Area("Graz", 47, 15, 25)])

    with pytest.raises(ValueError):
        remove_area_and_refresh(2)


def test_add_area_replaces_same_place(monkeypatch):
    saved = {}
    monkeypatch.setattr("tracker.store_discovery.geocode_area", lambda name: Area("Graz, Steiermark, Österreich", 47.0707, 15.4395, 0))
    monkeypatch.setattr(
        "tracker.store_discovery.config_areas",
        lambda: [Area("Graz, Steiermark, Österreich", 47.0707, 15.4395, 25)],
    )
    monkeypatch.setattr("tracker.store_discovery.discover_mediamarkt_stores", lambda areas: [])
    monkeypatch.setattr("tracker.store_discovery.save_areas_and_stores", lambda areas, stores: saved.update({"areas": areas, "stores": stores}))

    area, _ = add_area_and_refresh("Graz", 50)

    assert area.radius_km == 50
    assert len(saved["areas"]) == 1
    assert saved["areas"][0].radius_km == 50
