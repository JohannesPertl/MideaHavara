"""Telegram bot command polling.

This is intentionally a one-shot poller. A systemd timer can run it every
minute, which is simpler and more robust on a Raspberry Pi than keeping a
custom long-running bot daemon alive.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from .config import CONFIG_PATH, ROOT, STORES_PATH, Config, Secrets, load_config
from .notify import TIMEOUT, format_status_report, send_telegram
from .run import collect_buyable
from .store_discovery import add_area_and_refresh, clear_areas_and_refresh, config_areas, remove_area_and_refresh

log = logging.getLogger(__name__)

BOT_STATE_PATH = ROOT / "bot_state.json"
UPDATES_API = "https://api.telegram.org/bot{token}/getUpdates"
COMMANDS_API = "https://api.telegram.org/bot{token}/setMyCommands"


def _load_bot_state(path: Path = BOT_STATE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_bot_state(state: dict, path: Path = BOT_STATE_PATH) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _command_from_text(text: str) -> str:
    first = (text.strip().split() or [""])[0].lower()
    if not first.startswith("/"):
        return ""
    return first.split("@", 1)[0]


def _command_args(text: str) -> list[str]:
    parts = text.strip().split()
    return parts[1:]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _save_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _add_mediamarkt_store(args: list[str]) -> str:
    if len(args) < 4:
        return (
            "Syntax:\n"
            "/addstore <id> <lat> <lon> <name>\n\n"
            "Beispiel:\n"
            "/addstore 672 47.0617 15.4167 Graz Lazarettguertel"
        )
    store_id, lat_text, lon_text = args[0], args[1], args[2]
    name = " ".join(args[3:]).strip()
    try:
        lat = float(lat_text.replace(",", "."))
        lon = float(lon_text.replace(",", "."))
    except ValueError:
        return "Lat/Lon muessen Zahlen sein, z.B. 47.0617 15.4167."
    if not store_id.isdigit():
        return "Store-ID muss numerisch sein."
    if not name:
        return "Store-Name fehlt."

    data = _load_yaml(STORES_PATH)
    stores = list(data.get("mediamarkt") or [])
    replacement = {"id": store_id, "name": name, "lat": lat, "lon": lon}
    updated = False
    for i, store in enumerate(stores):
        if str((store or {}).get("id")) == store_id:
            stores[i] = replacement
            updated = True
            break
    if not updated:
        stores.append(replacement)
    data["mediamarkt"] = stores
    for chain in ("saturn", "obi", "bauhaus", "hornbach"):
        data.setdefault(chain, [])
    _save_yaml(STORES_PATH, data)

    action = "aktualisiert" if updated else "hinzugefuegt"
    return f"MediaMarkt-Store {name} (ID {store_id}) {action}."


def _remove_mediamarkt_store(args: list[str]) -> str:
    if len(args) != 1:
        return "Syntax: /removestore <id>"
    store_id = args[0]
    data = _load_yaml(STORES_PATH)
    stores = list(data.get("mediamarkt") or [])
    kept = [s for s in stores if str((s or {}).get("id")) != store_id]
    if len(kept) == len(stores):
        return f"Kein MediaMarkt-Store mit ID {store_id} gefunden."
    data["mediamarkt"] = kept
    _save_yaml(STORES_PATH, data)
    return f"MediaMarkt-Store ID {store_id} entfernt."


def _set_radius(args: list[str]) -> str:
    if len(args) != 1:
        return "Syntax: /radius <km>, z.B. /radius 80"
    try:
        radius = float(args[0].replace(",", "."))
    except ValueError:
        return "Radius muss eine Zahl sein."
    if radius <= 0 or radius > 500:
        return "Radius muss zwischen 1 und 500 km liegen."

    data = _load_yaml(CONFIG_PATH)
    location = dict(data.get("location") or {})
    location["radius_km"] = radius
    data["location"] = location
    _save_yaml(CONFIG_PATH, data)
    return f"Suchradius auf {radius:.0f} km gesetzt."


def _area_command(args: list[str]) -> str:
    if len(args) < 2:
        return (
            "Syntax:\n"
            "/area <Ort> <Radius-km>\n\n"
            "Beispiele:\n"
            "/area Graz 25\n"
            "/area Wien 40\n"
            "/area Bruck an der Mur 35"
        )
    radius_text = args[-1]
    place = " ".join(args[:-1]).strip()
    try:
        radius = float(radius_text.replace(",", "."))
    except ValueError:
        return "Radius muss eine Zahl sein, z.B. /area Graz 25."
    if radius <= 0 or radius > 500:
        return "Radius muss zwischen 1 und 500 km liegen."
    if not place:
        return "Ort fehlt."

    try:
        area, stores = add_area_and_refresh(place, radius)
    except Exception as exc:  # noqa: BLE001 - bot response should explain the failure
        log.exception("Area refresh failed")
        return f"Area konnte nicht gesetzt werden: {html.escape(str(exc))}"

    lines = [
        f"Area gesetzt: {html.escape(area.name)} ({area.radius_km:.0f} km)",
        f"Automatisch gefundene MediaMarkt-Filialen: {len(stores)}",
    ]
    for store in stores[:20]:
        lines.append(f"• {html.escape(store.name)} (ID {html.escape(store.id)}, ~{store.distance_km:.0f} km)")
    if len(stores) > 20:
        lines.append(f"... plus {len(stores) - 20} weitere.")
    return "\n".join(lines)


def _areas_command() -> str:
    areas = config_areas()
    if not areas:
        return "Keine Areas konfiguriert. Nutze /area <Ort> <Radius-km>."
    lines = ["<b>Konfigurierte Areas</b>"]
    for i, area in enumerate(areas, start=1):
        lines.append(f"{i}. {html.escape(area.name)} ({area.radius_km:.0f} km)")
    lines.append("")
    lines.append("Entfernen mit /removearea <nummer>, z.B. /removearea 2")
    return "\n".join(lines)


def _remove_area_command(args: list[str]) -> str:
    if len(args) != 1:
        return "Syntax: /removearea <nummer>. Nutze /areas fuer die Nummern."
    try:
        index = int(args[0])
    except ValueError:
        return "Die Area-Nummer muss eine ganze Zahl sein. Nutze /areas."
    try:
        removed, stores = remove_area_and_refresh(index)
    except Exception as exc:  # noqa: BLE001 - bot response should explain the failure
        log.exception("Area removal failed")
        return f"Area konnte nicht entfernt werden: {html.escape(str(exc))}"
    return (
        f"Area entfernt: {html.escape(removed.name)}.\n"
        f"Automatisch gefundene MediaMarkt-Filialen verbleibend: {len(stores)}"
    )


def _clear_areas_command() -> str:
    clear_areas_and_refresh()
    return "Alle Areas und automatisch gefundenen MediaMarkt-Stores wurden entfernt."


def _stores_report(cfg: Config) -> str:
    loc = cfg.location
    lines = [
        "🏬 <b>Welche Shops und Filialen werden geprüft?</b>",
        f"Standort: {html.escape(loc.city)} {html.escape(loc.postal_code)} "
        f"({loc.radius_km:.0f} km Umkreis)",
        "",
        "<b>Aktive Quellen</b>",
    ]
    labels = {
        "geizhals": "Geizhals AT - Online/Preisvergleich",
        "mediamarkt": "MediaMarkt AT - Online + Filialbestand",
        "obi": "OBI AT - Online-Produktseite",
        "bauhaus": "BAUHAUS AT - Online-Produktseite",
    }
    for source in cfg.enabled_sources():
        lines.append(f"• {html.escape(labels.get(source, source))}")

    stores = [s for s in cfg.stores_for("mediamarkt") if s.id]
    if stores:
        lines.extend(["", "<b>MediaMarkt-Filialbestand</b>"])
        for store in stores:
            lines.append(f"• {html.escape(store.name)} (ID {html.escape(store.id)})")

    lines.extend(
        [
            "",
            "Hinweis: OBI und BAUHAUS werden aktuell online geprüft; "
            "Filialbestand wird nur für MediaMarkt abgefragt.",
        ]
    )
    return "\n".join(lines)


def _status_report(cfg: Config) -> str:
    products = "\n".join(
        f"• {html.escape(p.name)} bis <b>{p.max_price:.2f} €</b>" for p in cfg.products
    )
    sources = ", ".join(html.escape(s) for s in cfg.enabled_sources())
    return (
        "✅ <b>MideaHavara läuft</b>\n\n"
        f"<b>Produkte</b>\n{products}\n\n"
        f"<b>Aktive Quellen</b>\n{sources}\n\n"
        "Kommandos:\n"
        "/stores - Shops und Filialen anzeigen\n"
        "/check - echten Live-Check ausführen und Ergebnis senden\n"
        "/addstore - MediaMarkt-Store hinzufuegen\n"
        "/removestore - MediaMarkt-Store entfernen\n"
        "/radius - Suchradius setzen\n"
        "/test - Antworttest senden\n"
        "/help - Hilfe anzeigen"
    )


def _help_text() -> str:
    return (
        "<b>MideaHavara Bot</b>\n\n"
        "/status - schnelle Statusübersicht\n"
        "/stores - aktive Quellen und MediaMarkt-Filialen\n"
        "/check - Live-Check ausführen und Ergebnis melden\n"
        "/addstore <id> <lat> <lon> <name> - MediaMarkt-Store hinzufuegen\n"
        "/removestore <id> - MediaMarkt-Store entfernen\n"
        "/radius <km> - Suchradius setzen\n"
        "/test - Bot-Antwort testen\n"
        "/help - diese Hilfe"
    )


def _get_updates(secrets: Secrets, offset: int | None) -> list[dict]:
    url = UPDATES_API.format(token=secrets.telegram_bot_token)
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {data}")
    return data.get("result") or []


def _set_commands(secrets: Secrets) -> bool:
    if not secrets.telegram_configured:
        log.error("Telegram nicht konfiguriert.")
        return False
    url = COMMANDS_API.format(token=secrets.telegram_bot_token)
    commands = [
        {"command": "status", "description": "Schnelle Statusuebersicht"},
        {"command": "stores", "description": "Shops und Filialen anzeigen"},
        {"command": "check", "description": "Live-Check ausfuehren"},
        {"command": "addstore", "description": "MediaMarkt-Store hinzufuegen"},
        {"command": "removestore", "description": "MediaMarkt-Store entfernen"},
        {"command": "radius", "description": "Suchradius setzen"},
        {"command": "test", "description": "Antworttest senden"},
        {"command": "help", "description": "Hilfe anzeigen"},
    ]
    resp = requests.post(url, json={"commands": commands}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram setMyCommands failed: %s", data)
        return False
    return True


def _status_report(cfg: Config) -> str:
    products = "\n".join(
        f"• {html.escape(p.name)} bis <b>{p.max_price:.2f} EUR</b>" for p in cfg.products
    )
    sources = ", ".join(html.escape(s) for s in cfg.enabled_sources())
    return (
        "✅ <b>MideaHavara laeuft</b>\n\n"
        f"<b>Produkte</b>\n{products}\n\n"
        f"<b>Aktive Quellen</b>\n{sources}\n\n"
        "Kommandos:\n"
        "/stores - Shops und Filialen anzeigen\n"
        "/check - echten Live-Check ausfuehren und Ergebnis senden\n"
        "/area - Ort + Radius setzen und Stores automatisch finden\n"
        "/areas - konfigurierte Areas anzeigen\n"
        "/removearea - einzelne Area entfernen\n"
        "/clearareas - Areas und Stores leeren\n"
        "/test - Antworttest senden\n"
        "/help - Hilfe anzeigen"
    )


def _help_text() -> str:
    return (
        "<b>MideaHavara Bot</b>\n\n"
        "/status - schnelle Statusuebersicht\n"
        "/stores - aktive Quellen und MediaMarkt-Filialen\n"
        "/check - Live-Check ausfuehren und Ergebnis melden\n"
        "/area <Ort> <Radius-km> - Stores automatisch finden\n"
        "/areas - konfigurierte Areas anzeigen\n"
        "/removearea <nummer> - einzelne Area entfernen\n"
        "/clearareas - Areas und Stores leeren\n"
        "/test - Bot-Antwort testen\n"
        "/help - diese Hilfe"
    )


def _set_commands(secrets: Secrets) -> bool:
    if not secrets.telegram_configured:
        log.error("Telegram nicht konfiguriert.")
        return False
    url = COMMANDS_API.format(token=secrets.telegram_bot_token)
    commands = [
        {"command": "status", "description": "Schnelle Statusuebersicht"},
        {"command": "stores", "description": "Shops und Filialen anzeigen"},
        {"command": "check", "description": "Live-Check ausfuehren"},
        {"command": "area", "description": "Ort und Radius setzen"},
        {"command": "areas", "description": "Konfigurierte Areas anzeigen"},
        {"command": "removearea", "description": "Einzelne Area entfernen"},
        {"command": "clearareas", "description": "Areas und Stores leeren"},
        {"command": "test", "description": "Antworttest senden"},
        {"command": "help", "description": "Hilfe anzeigen"},
    ]
    resp = requests.post(url, json={"commands": commands}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        log.error("Telegram setMyCommands failed: %s", data)
        return False
    return True


def _handle_command(cfg: Config, secrets: Secrets, command: str, message_id: int, text: str = "") -> None:
    if command in {"", "/help", "/start"}:
        send_telegram(_help_text(), secrets, reply_to_message_id=message_id)
        return
    if command == "/status":
        send_telegram(_status_report(cfg), secrets, reply_to_message_id=message_id)
        return
    if command == "/stores":
        send_telegram(_stores_report(cfg), secrets, reply_to_message_id=message_id)
        return
    if command == "/area":
        send_telegram(
            "⏳ Suche MediaMarkt-Filialen fuer diese Area. Das kann kurz dauern.",
            secrets,
            reply_to_message_id=message_id,
        )
        send_telegram(_area_command(_command_args(text)), secrets, reply_to_message_id=message_id)
        return
    if command == "/areas":
        send_telegram(_areas_command(), secrets, reply_to_message_id=message_id)
        return
    if command == "/removearea":
        send_telegram(
            "⏳ Entferne Area und aktualisiere Stores.",
            secrets,
            reply_to_message_id=message_id,
        )
        send_telegram(_remove_area_command(_command_args(text)), secrets, reply_to_message_id=message_id)
        return
    if command == "/clearareas":
        send_telegram(_clear_areas_command(), secrets, reply_to_message_id=message_id)
        return
    if command == "/addstore":
        send_telegram(_add_mediamarkt_store(_command_args(text)), secrets, reply_to_message_id=message_id)
        return
    if command == "/removestore":
        send_telegram(_remove_mediamarkt_store(_command_args(text)), secrets, reply_to_message_id=message_id)
        return
    if command == "/radius":
        send_telegram(_set_radius(_command_args(text)), secrets, reply_to_message_id=message_id)
        return
    if command == "/test":
        send_telegram(
            "✅ Bot-Antwort funktioniert.",
            secrets,
            reply_to_message_id=message_id,
        )
        return
    if command == "/check":
        send_telegram(
            "⏳ Live-Check gestartet. Das kann auf dem Pi etwas dauern.",
            secrets,
            reply_to_message_id=message_id,
        )
        _, summary = collect_buyable(cfg)
        send_telegram(
            format_status_report(cfg, summary, datetime.now(timezone.utc)),
            secrets,
            reply_to_message_id=message_id,
        )
        return

    send_telegram(
        "Unbekanntes Kommando. Nutze /help.",
        secrets,
        reply_to_message_id=message_id,
    )


def poll_once() -> int:
    cfg = load_config()
    secrets = Secrets.from_env()
    if not secrets.telegram_configured:
        log.warning("Telegram nicht konfiguriert; Bot-Poll übersprungen.")
        return 0

    state = _load_bot_state()
    offset = state.get("telegram_update_offset")
    updates = _get_updates(secrets, offset if isinstance(offset, int) else None)

    chat_id = str(secrets.telegram_chat_id)
    max_update_id = offset - 1 if isinstance(offset, int) else None
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

        msg = update.get("message") if isinstance(update, dict) else None
        if not isinstance(msg, dict):
            continue
        chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        if str(chat.get("id")) != chat_id:
            continue
        text = str(msg.get("text") or "")
        message_id = msg.get("message_id")
        if not isinstance(message_id, int):
            continue
        command = _command_from_text(text)
        if command:
            log.info("Telegram-Kommando: %s", command)
            _handle_command(cfg, secrets, command, message_id, text)

    if max_update_id is not None:
        state["telegram_update_offset"] = max_update_id + 1
        _save_bot_state(state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Telegram command poller")
    parser.add_argument("--set-commands", action="store_true", help="Telegram command menu setzen")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.set_commands:
        return 0 if _set_commands(Secrets.from_env()) else 1
    return poll_once()


if __name__ == "__main__":
    sys.exit(main())
