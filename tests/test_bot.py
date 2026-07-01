from tracker.bot import _command_from_text, _stores_report
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


def test_stores_report_lists_enabled_sources_and_stores():
    msg = _stores_report(_cfg())

    assert "Geizhals AT" in msg
    assert "MediaMarkt AT" in msg
    assert "OBI AT" in msg
    assert "BAUHAUS AT" in msg
    assert "Graz Lazarettguertel (ID 672)" in msg
    assert "Graz Liebenau (ID 849)" in msg
