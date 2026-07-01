"""Telegram-Benachrichtigung."""

from __future__ import annotations

import html
import logging
import sys
from collections import defaultdict

import requests

from .config import Secrets
from .models import CHANNEL_STORE, CONDITION_NEW, Offer

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
TIMEOUT = 20


def send_telegram(text: str, secrets: Secrets, *, reply_to_message_id: int | None = None) -> bool:
    """Sendet eine Nachricht. Gibt True bei Erfolg zurück."""
    if not secrets.telegram_configured:
        log.warning("Telegram nicht konfiguriert (Token/Chat-ID fehlen) – überspringe Versand.")
        return False
    url = TELEGRAM_API.format(token=secrets.telegram_bot_token)
    payload = {
        "chat_id": secrets.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Telegram-Versand fehlgeschlagen: %s", exc)
        return False
    return True


def latest_message_id(secrets: Secrets) -> int | None:
    """Return the latest message id in the configured chat, if getUpdates has one."""
    if not secrets.telegram_configured:
        log.warning("Telegram nicht konfiguriert (Token/Chat-ID fehlen).")
        return None
    url = TELEGRAM_UPDATES_API.format(token=secrets.telegram_bot_token)
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Telegram-getUpdates fehlgeschlagen: %s", exc)
        return None
    if not data.get("ok"):
        log.error("Telegram-getUpdates meldete Fehler: %s", data)
        return None

    chat_id = str(secrets.telegram_chat_id)
    for update in reversed(data.get("result") or []):
        msg = update.get("message") if isinstance(update, dict) else None
        chat = msg.get("chat") if isinstance(msg, dict) else None
        if str((chat or {}).get("id")) == chat_id and msg.get("message_id") is not None:
            return int(msg["message_id"])
    return None


def _offer_line(o: Offer) -> list[str]:
    """Zwei HTML-Zeilen für ein Angebot. Alle Fremdtexte werden escaped,
    damit ein gescrapeter Titel/Händler mit '<', '>' oder '&' die
    HTML-Nachricht nicht zerschießt (Telegram würde sie sonst mit 400
    ablehnen – ausgerechnet im Alarm-Moment)."""
    cond = "Neu" if o.condition == CONDITION_NEW else "Gebraucht"
    where = html.escape(o.merchant or o.source.capitalize())
    loc = ""
    if o.channel == CHANNEL_STORE and o.store_name:
        dist = f", ~{o.distance_km:.0f} km" if o.distance_km is not None else ""
        loc = f" 🏬 Filiale {html.escape(o.store_name)}{dist}"
    return [
        f"• <b>{o.price:.2f} €</b> – {where} [{cond}]{loc}",
        f"  <a href=\"{html.escape(o.url, quote=True)}\">Zum Angebot</a>",
    ]


def format_offers(offers: list[Offer]) -> str:
    """Baut eine kompakte HTML-Nachricht, nach Produkt gruppiert."""
    by_product: dict[str, list[Offer]] = defaultdict(list)
    for o in offers:
        by_product[o.product_name or o.title].append(o)

    lines: list[str] = []
    for product_name, group in by_product.items():
        n = len(group)
        lines.append(
            f"🟢 <b>{html.escape(product_name)}</b> verfügbar! "
            f"({n} neue{'s' if n == 1 else ''} Angebot{'e' if n != 1 else ''})"
        )
        lines.append("")
        for o in sorted(group, key=lambda x: x.price):
            lines.extend(_offer_line(o))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_heartbeat(summary, now) -> str:
    """Tägliche "lebt noch"-Statusmeldung mit günstigstem Preis je Gerät.

    ``summary`` ist ein run.RunSummary, ``now`` ein datetime (für den Zeitstempel).
    Bewusst lose typisiert, um einen Importzyklus run<->notify zu vermeiden.
    """
    ts = now.strftime("%d.%m.%Y %H:%M UTC")
    lines = [
        f"💓 <b>Tracker aktiv</b> – {ts}",
        f"Quellen mit Daten: {summary.sources_with_data}/{summary.attempts}",
        f"Bestellbar im Budget: {summary.buyable_count}",
    ]
    if summary.best_by_product:
        lines.append("")
        lines.append("Günstigster Preis je Gerät:")
        for name, (price, merchant) in sorted(summary.best_by_product.items()):
            lines.append(
                f"• {html.escape(name)}: <b>{price:.2f} €</b> ({html.escape(merchant)})"
            )
    else:
        lines.append("")
        lines.append("Aktuell kein Preis für die beobachteten Geräte gefunden.")
    return "\n".join(lines)


def format_outage(summary) -> str:
    """Alarm, wenn KEINE Quelle Daten lieferte (wahrscheinlich alles geblockt)."""
    return (
        "⚠️ <b>Tracker: Totalausfall</b>\n\n"
        f"Keine einzige Quelle lieferte Daten ({summary.attempts} Abrufversuche). "
        "Vermutlich sind die Shops gerade alle geblockt oder es gibt ein Problem. "
        "Solange dieser Zustand anhält, kann eine echte Verfügbarkeit übersehen werden."
    )


def _self_test() -> int:
    """`python -m tracker.notify --test` schickt eine Testnachricht."""
    logging.basicConfig(level=logging.INFO)
    secrets = Secrets.from_env()
    ok = send_telegram(
        "✅ Testnachricht vom Midea PortaSplit Tracker – Benachrichtigungen funktionieren.",
        secrets,
    )
    print("Gesendet." if ok else "Fehlgeschlagen (siehe Log / Secrets prüfen).")
    return 0 if ok else 1


def _reply_test() -> int:
    """`python -m tracker.notify --reply-test` replies to the latest incoming chat message."""
    logging.basicConfig(level=logging.INFO)
    secrets = Secrets.from_env()
    message_id = latest_message_id(secrets)
    if message_id is None:
        print("Keine passende Telegram-Nachricht gefunden. Sende dem Bot zuerst /start oder hi.")
        return 1
    ok = send_telegram(
        "✅ Reply-Test vom Midea PortaSplit Tracker - Bot-Verbindung funktioniert.",
        secrets,
        reply_to_message_id=message_id,
    )
    print("Reply gesendet." if ok else "Reply fehlgeschlagen (siehe Log / Secrets prüfen).")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--reply-test" in sys.argv:
        raise SystemExit(_reply_test())
    if "--test" in sys.argv:
        raise SystemExit(_self_test())
    print("Nutze: python -m tracker.notify --test oder --reply-test")
