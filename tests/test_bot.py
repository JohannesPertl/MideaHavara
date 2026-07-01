from tracker.bot import _add_mediamarkt_store, _command_args, _command_from_text, _remove_mediamarkt_store, _set_radius, _stores_report
from tracker.config import Config, Location, Product, Store


def _cfg() -> Config:
    return Config(
        products=[Product("Midea PortaSplit 12.000 BTU", ["4048164116478"], ["portasplit"], [], 1000, True)],
        location=Location("8010", "Graz", 47.0707, 15.4395, 25),
        sources={"geizhals": True, "mediamarkt": True, "obi": True, "bauhaus": True},
        stores={
            "mediamarkt": [
                Store("mediamarkt", "672", "Graz Lazarettguertel"),
                Store("mediamarkt", "849", "Graz Liebenau"),
            ]
        },
    )


def test_command_from_text_handles_bot_username():
    assert _command_from_text("/stores@MideaHavaraBot please") == "/stores"
    assert _command_from_text("/CHECK") == "/check"
    assert _command_from_text("hello") == ""
    assert _command_args("/addstore 1 2 3 Name") == ["1", "2", "3", "Name"]


def test_stores_report_lists_enabled_sources_and_stores():
    msg = _stores_report(_cfg())

    assert "Geizhals AT" in msg
    assert "MediaMarkt AT" in msg
    assert "OBI AT" in msg
    assert "BAUHAUS AT" in msg
    assert "Graz Lazarettguertel (ID 672)" in msg
    assert "Graz Liebenau (ID 849)" in msg


def test_add_remove_store_updates_yaml(monkeypatch, tmp_path):
    stores_path = tmp_path / "stores.yaml"
    stores_path.write_text("mediamarkt: []\nsaturn: []\n", encoding="utf-8")
    monkeypatch.setattr("tracker.bot.STORES_PATH", stores_path)

    msg = _add_mediamarkt_store(["123", "47.1", "15.2", "Test", "Store"])
    assert "hinzugefuegt" in msg
    text = stores_path.read_text(encoding="utf-8")
    assert 'id: "123"' in text or "id: '123'" in text or "id: 123" in text
    assert "Test Store" in text

    msg = _remove_mediamarkt_store(["123"])
    assert "entfernt" in msg
    assert "Test Store" not in stores_path.read_text(encoding="utf-8")


def test_set_radius_updates_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "location:\n  city: Graz\n  postal_code: '8010'\n  latitude: 47.0\n  longitude: 15.0\n  radius_km: 25\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("tracker.bot.CONFIG_PATH", config_path)

    msg = _set_radius(["80"])

    assert "80 km" in msg
    assert "radius_km: 80.0" in config_path.read_text(encoding="utf-8")
