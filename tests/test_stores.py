"""Tests für den Filial-Pfad (MediaMarkt/Saturn) mit gemockter Bestands-API.

Die Tests beweisen ohne Netz, dass aus einer hinterlegten Filiale + positivem
API-Bestand ein Filial-Angebot mit korrekt berechneter Distanz entsteht – und
dass Einträge ohne Store-ID sauber übersprungen werden (kein Fehlalarm).
"""

from tracker.config import Config, Location, Product, Store
from tracker.models import CHANNEL_STORE
from tracker.sources import mediamarkt

URL = "https://www.mediamarkt.de/de/product/_midea-porta-split-142245268.html"
AT_URL = "https://www.mediamarkt.at/de/product/_midea-portasplit-mobile-klimaanlage-2075674.html"


def _cfg(stores: list[Store]) -> Config:
    return Config(
        products=[
            Product(
                name="Midea PortaSplit 12.000 BTU",
                eans=["4048164116478"],
                title_must_include=["portasplit"],
                title_must_exclude=["comfee"],
                max_price=800.0,
                allow_used=True,
            )
        ],
        location=Location("74321", "Bietigheim-Bissingen", 48.9543, 9.1316, 25.0),
        sources={"mediamarkt": True},
        stores={"mediamarkt": stores},
    )


def test_store_offer_with_distance(monkeypatch):
    store = Store(chain="mediamarkt", id="S123", name="Ludwigsburg", lat=48.9018, lon=9.1660)
    cfg = _cfg([store])

    def fake_json(endpoint, **kwargs):
        return {"data": {"availabilities": [{"storeId": "S123", "availabilityType": "IN_STORE"}]}}

    monkeypatch.setattr(mediamarkt, "http_get_json", fake_json)

    offers = mediamarkt._store_offers(cfg, cfg.product, "mediamarkt", URL, online_price=699.0)
    assert len(offers) == 1
    o = offers[0]
    assert o.channel == CHANNEL_STORE
    assert o.store_name == "Ludwigsburg"
    assert o.price == 699.0
    # Distanz Bietigheim-Bissingen -> Ludwigsburg ~6-8 km, berechnet.
    assert o.distance_km is not None and 3 < o.distance_km < 12


def test_store_without_id_is_skipped(monkeypatch):
    store = Store(chain="mediamarkt", id="", name="Stuttgart", lat=48.79, lon=9.18)
    cfg = _cfg([store])

    called = {"n": 0}

    def fake_json(endpoint, **kwargs):
        called["n"] += 1
        return {"data": {"availabilities": []}}

    monkeypatch.setattr(mediamarkt, "http_get_json", fake_json)

    offers = mediamarkt._store_offers(cfg, cfg.product, "mediamarkt", URL, online_price=699.0)
    assert offers == []
    # Ohne abfragbare Store-ID wird die API gar nicht erst kontaktiert.
    assert called["n"] == 0


def test_browser_fallback_used_when_http_blocked(monkeypatch):
    # http_get_json simuliert die Bot-Wall (403 -> None); der Browser-Fallback
    # liefert dann das JSON und es entsteht trotzdem ein Filial-Angebot.
    store = Store(chain="mediamarkt", id="450", name="Ludwigsburg", lat=48.9080, lon=9.1720)
    cfg = _cfg([store])

    monkeypatch.setattr(mediamarkt, "http_get_json", lambda *a, **k: None)

    captured = {}

    def fake_browser(full_url, **kwargs):
        captured["url"] = full_url
        captured["referer"] = kwargs.get("referer")
        return {"data": {"availabilities": [{"storeId": "450", "availabilityType": "IN_STORE"}]}}

    monkeypatch.setattr(mediamarkt, "fetch_json_via_browser", fake_browser)

    offers = mediamarkt._store_offers(cfg, cfg.product, "mediamarkt", URL, online_price=699.0)
    assert len(offers) == 1
    assert offers[0].store_name == "Ludwigsburg"
    # Die API-URL trägt die Produkt-ID, der Referer ist die Produktseite.
    assert "142245268" in captured["url"]
    assert captured["referer"] == URL


def test_austrian_product_url_uses_at_graphql_context(monkeypatch):
    store = Store(chain="mediamarkt", id="672", name="Graz Lazarettguertel", lat=47.0617, lon=15.4167)
    cfg = _cfg([store])

    captured = {}

    def fake_json(endpoint, **kwargs):
        captured["endpoint"] = endpoint
        captured["params"] = kwargs.get("params") or {}
        return {"data": {"availabilities": []}}

    monkeypatch.setattr(mediamarkt, "http_get_json", fake_json)

    offers = mediamarkt._store_offers(cfg, cfg.product, "mediamarkt", AT_URL, online_price=899.0)
    assert offers == []
    assert captured["endpoint"] == "https://www.mediamarkt.at/api/v1/graphql"
    assert '"country":"AT"' in captured["params"]["extensions"]


def test_store_not_in_stock_no_offer(monkeypatch):
    store = Store(chain="mediamarkt", id="S123", name="Ludwigsburg", lat=48.9018, lon=9.1660)
    cfg = _cfg([store])

    def fake_json(endpoint, **kwargs):
        return {"data": {"availabilities": [{"storeId": "S123", "availabilityType": "NOT_AVAILABLE"}]}}

    monkeypatch.setattr(mediamarkt, "http_get_json", fake_json)

    offers = mediamarkt._store_offers(cfg, cfg.product, "mediamarkt", URL, online_price=699.0)
    assert offers == []
