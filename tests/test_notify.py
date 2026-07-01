"""Tests für die Telegram-Nachricht: HTML-Escaping + Produkt-Gruppierung."""

from tracker.models import CHANNEL_ONLINE, CHANNEL_STORE, CONDITION_NEW, CONDITION_USED, Offer
from datetime import datetime, timezone

from tracker.config import Config, Location, Product, Secrets, Store
from tracker.notify import format_offers, format_status_report, latest_message_id, send_telegram
from tracker.run import RunSummary


def _offer(**kw) -> Offer:
    base = dict(
        source="obi",
        title="Midea PortaSplit",
        price=699.0,
        url="https://example.com/x",
        in_stock=True,
        condition=CONDITION_NEW,
        channel=CHANNEL_ONLINE,
        merchant="OBI",
        product_name="Midea PortaSplit 12.000 BTU",
    )
    base.update(kw)
    return Offer(**base)


def test_escapes_html_in_merchant_and_url():
    # Titel/Händler mit Sonderzeichen dürfen die HTML-Nachricht NICHT zerstören.
    msg = format_offers([_offer(merchant="Müller & Sohn <Markt>", url="https://x/?a=1&b=2")])
    assert "Müller &amp; Sohn &lt;Markt&gt;" in msg
    assert "a=1&amp;b=2" in msg
    # Keine unescapeten gefährlichen Sequenzen im Fremdtext.
    assert "<Markt>" not in msg


def test_groups_offers_by_product():
    offers = [
        _offer(product_name="Gerät A", merchant="OBI", price=700),
        _offer(product_name="Gerät B", merchant="Idealo", price=500),
        _offer(product_name="Gerät A", merchant="Saturn", price=650),
    ]
    msg = format_offers(offers)
    assert "<b>Gerät A</b>" in msg
    assert "<b>Gerät B</b>" in msg
    # Gerät A hat 2 Angebote.
    assert "2 neue Angebote" in msg
    assert "1 neues Angebot" in msg


def test_store_offer_shows_distance():
    o = _offer(channel=CHANNEL_STORE, store_name="Ludwigsburg", distance_km=8.0, merchant="Saturn")
    msg = format_offers([o])
    assert "Filiale Ludwigsburg" in msg
    assert "~8 km" in msg


def test_used_condition_labeled():
    msg = format_offers([_offer(condition=CONDITION_USED, merchant="Amazon Warehouse")])
    assert "[Gebraucht]" in msg


class _Resp:
    def __init__(self, data=None):
        self.data = data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


def test_send_telegram_can_reply(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr("tracker.notify.requests.post", fake_post)
    secrets = Secrets(telegram_bot_token="tok", telegram_chat_id="123")

    assert send_telegram("hello", secrets, reply_to_message_id=42) is True
    assert captured["json"]["chat_id"] == "123"
    assert captured["json"]["reply_parameters"] == {"message_id": 42}


def test_latest_message_id_uses_configured_chat(monkeypatch):
    def fake_get(url, timeout):
        return _Resp(
            {
                "ok": True,
                "result": [
                    {"message": {"message_id": 1, "chat": {"id": 999}}},
                    {"message": {"message_id": 2, "chat": {"id": 123}}},
                    {"message": {"message_id": 3, "chat": {"id": 123}}},
                ],
            }
        )

    monkeypatch.setattr("tracker.notify.requests.get", fake_get)
    secrets = Secrets(telegram_bot_token="tok", telegram_chat_id="123")

    assert latest_message_id(secrets) == 3


def test_status_report_lists_sources_and_stores():
    cfg = Config(
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
    summary = RunSummary(attempts=4, sources_with_data=2, buyable_count=0)

    msg = format_status_report(cfg, summary, datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc))

    assert "Graz 8010" in msg
    assert "MediaMarkt AT - Online + Filialbestand" in msg
    assert "OBI AT - Online-Produktseite" in msg
    assert "Graz Lazarettguertel (ID 672)" in msg
    assert "Graz Liebenau (ID 849)" in msg
    assert "Quellen mit Daten: 2/4" in msg
