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

from .config import ROOT, Config, Secrets, load_config
from .notify import TIMEOUT, format_status_report, send_telegram
from .run import collect_buyable

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
        "/test - Antworttest senden\n"
        "/help - Hilfe anzeigen"
    )


def _help_text() -> str:
    return (
        "<b>MideaHavara Bot</b>\n\n"
        "/status - schnelle Statusübersicht\n"
        "/stores - aktive Quellen und MediaMarkt-Filialen\n"
        "/check - Live-Check ausführen und Ergebnis melden\n"
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


def _handle_command(cfg: Config, secrets: Secrets, command: str, message_id: int) -> None:
    if command in {"", "/help", "/start"}:
        send_telegram(_help_text(), secrets, reply_to_message_id=message_id)
        return
    if command == "/status":
        send_telegram(_status_report(cfg), secrets, reply_to_message_id=message_id)
        return
    if command == "/stores":
        send_telegram(_stores_report(cfg), secrets, reply_to_message_id=message_id)
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
            _handle_command(cfg, secrets, command, message_id)

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
