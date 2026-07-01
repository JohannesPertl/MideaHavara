import json

from tracker.store_discovery import _hydration_json, _jsonld_geo, config_areas


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
