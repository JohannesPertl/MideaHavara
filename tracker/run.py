"""Einstiegspunkt: Quellen abfragen, filtern, Diff bilden, benachrichtigen.

Aufruf:
    python -m tracker.run            # echter Lauf (sendet Push, schreibt State)
    python -m tracker.run --dry-run  # nur loggen, kein Versand, kein State-Write
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from .config import Config, Product, Secrets, load_config
from .matching import is_buyable, matches_product
from .models import CHANNEL_ONLINE, CONDITION_NEW, Offer
from .notify import format_heartbeat, format_offers, format_outage, format_status_report, send_telegram
from .sources import get_source
from .state import diff_new, load_state, save_state

log = logging.getLogger(__name__)


@dataclass
class RunSummary:
    """Diagnose-Kennzahlen eines Laufs – Basis für Heartbeat/Totalausfall."""

    attempts: int = 0  # Quelle×Produkt-Abrufversuche gesamt
    sources_with_data: int = 0  # davon mit >= 1 Angebot
    buyable_count: int = 0
    # product_name -> (bester Preis des RICHTIGEN Produkts, Händler)
    best_by_product: dict[str, tuple[float, str]] = field(default_factory=dict)

    def note_best(self, product_name: str, price: float, merchant: str) -> None:
        cur = self.best_by_product.get(product_name)
        if cur is None or price < cur[0]:
            self.best_by_product[product_name] = (price, merchant)


def collect_offers_for_product(
    cfg: Config, product: Product, summary: RunSummary
) -> list[Offer]:
    """Fragt alle aktivierten Quellen für EIN Produkt ab. Fehler pro Quelle isolieren."""
    all_offers: list[Offer] = []
    for name in cfg.enabled_sources():
        fn = get_source(name)
        if fn is None:
            log.warning("Unbekannte Quelle '%s' – übersprungen.", name)
            continue
        summary.attempts += 1
        try:
            offers = fn(cfg, product)
        except Exception as exc:  # noqa: BLE001 - eine Quelle darf nicht den Lauf kippen
            log.error("Quelle '%s' (%s) fehlgeschlagen: %s", name, product.name, exc, exc_info=True)
            continue
        if offers:
            summary.sources_with_data += 1
        for o in offers:
            # Günstigsten Preis des RICHTIGEN Produkts merken (auch über Budget).
            if matches_product(o, product) and o.price is not None:
                summary.note_best(product.name, o.price, o.merchant or o.source)
            # Angebot mit dem konfigurierten Produktnamen taggen (Gruppierung).
            all_offers.append(replace(o, product_name=product.name))
    return all_offers


def collect_buyable(cfg: Config) -> tuple[list[Offer], RunSummary]:
    """Sammelt über die ganze Watchlist alle wirklich bestellbaren Angebote."""
    buyable: list[Offer] = []
    summary = RunSummary()
    for product in cfg.products:
        offers = collect_offers_for_product(cfg, product, summary)
        kept = [o for o in offers if is_buyable(o, product, cfg.location)]
        log.info("'%s': %d Angebote, davon %d wirklich bestellbar < %.0f €.",
                 product.name, len(offers), len(kept), product.max_price)
        buyable.extend(kept)
    summary.buyable_count = len(buyable)
    return buyable, summary


def _maybe_heartbeat(
    cfg: Config, state: dict, summary: RunSummary, secrets, *, dry_run: bool
) -> None:
    """Schickt höchstens 1×/Tag eine Statusmeldung; bei Totalausfall einen Alarm.

    Beides wird über Datumsmarken in ``state`` entprellt, damit der 10-Min-Takt
    nicht spammt.
    """
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    # Totalausfall: kein einziger Abruf lieferte Daten -> wahrscheinlich alles
    # geblockt. Sofort (aber nur 1×/Tag) melden, da sonst falsche Sicherheit.
    if summary.attempts > 0 and summary.sources_with_data == 0:
        if state.get("last_outage_alert") != today:
            log.warning("Totalausfall erkannt (%d Versuche, 0 mit Daten).", summary.attempts)
            msg = format_outage(summary)
            if dry_run:
                print("--- DRY RUN: Totalausfall-Alarm ---\n" + msg)
            elif send_telegram(msg, secrets):
                state["last_outage_alert"] = today
        return  # bei Totalausfall keinen normalen Heartbeat senden

    if not cfg.heartbeat_enabled:
        return
    if now.hour < cfg.heartbeat_hour_utc or state.get("last_heartbeat") == today:
        return
    log.info("Sende täglichen Heartbeat.")
    msg = format_heartbeat(summary, now)
    if dry_run:
        print("--- DRY RUN: Heartbeat ---\n" + msg)
    elif send_telegram(msg, secrets):
        state["last_heartbeat"] = today


def run(dry_run: bool = False) -> int:
    cfg = load_config()
    secrets = Secrets.from_env()

    names = ", ".join(p.name for p in cfg.products)
    log.info("Starte Check für %d Produkt(e) [%s] (Quellen: %s)",
             len(cfg.products), names, ", ".join(cfg.enabled_sources()))

    buyable, summary = collect_buyable(cfg)
    for o in buyable:
        log.info("  ✓ %s", o.describe())

    state = load_state()
    seen = set(state.get("available_keys", []))
    new_offers, current_keys = diff_new(buyable, seen)

    if new_offers:
        log.info("%d NEUE verfügbare Angebote -> Benachrichtigung.", len(new_offers))
        message = format_offers(new_offers)
        if dry_run:
            print("--- DRY RUN: Telegram-Nachricht ---")
            print(message)
        else:
            send_telegram(message, secrets)
    else:
        log.info("Keine neuen verfügbaren Angebote.")

    _maybe_heartbeat(cfg, state, summary, secrets, dry_run=dry_run)

    state["available_keys"] = sorted(current_keys)
    if not dry_run:
        save_state(state)
        log.info("State aktualisiert (%d verfügbare Angebote gemerkt).", len(current_keys))

    return 0


def run_status_report() -> int:
    """Runs a live check and sends a Telegram report without changing alert state."""
    cfg = load_config()
    secrets = Secrets.from_env()

    names = ", ".join(p.name for p in cfg.products)
    log.info("Starte Status-Check fuer %d Produkt(e) [%s] (Quellen: %s)",
             len(cfg.products), names, ", ".join(cfg.enabled_sources()))

    _, summary = collect_buyable(cfg)
    message = format_status_report(cfg, summary, datetime.now(timezone.utc))
    ok = send_telegram(message, secrets)
    if ok:
        log.info("Statusbericht gesendet.")
        return 0
    log.error("Statusbericht konnte nicht gesendet werden.")
    return 1


def run_demo() -> int:
    """Schickt einen einmaligen Beispiel-Alarm im echten Format (Funktionstest)."""
    cfg = load_config()
    secrets = Secrets.from_env()
    product = cfg.product
    demo = Offer(
        source="hornbach",
        title=f"{product.name} (BEISPIEL/Test)",
        price=699.0,
        url=product.url_for("hornbach")
        or "https://www.hornbach.de/p/klimasplitgeraet-midea-portasplit-12-000-btu-105-m-weiss/12356554/",
        in_stock=True,
        condition=CONDITION_NEW,
        channel=CHANNEL_ONLINE,
        ean=product.eans[0] if product.eans else None,
        merchant="Hornbach (Test-Alarm)",
        product_name=product.name,
    )
    message = "🔔 <b>TEST-ALARM</b> – so sieht eine echte Benachrichtigung aus:\n\n" + format_offers(
        [demo]
    )
    ok = send_telegram(message, secrets)
    log.info("Test-Alarm gesendet." if ok else "Test-Alarm fehlgeschlagen (Secrets prüfen).")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Midea PortaSplit Verfügbarkeits-Check")
    parser.add_argument("--dry-run", action="store_true", help="Nur loggen, nichts senden/schreiben")
    parser.add_argument("--demo", action="store_true", help="Einmaligen Beispiel-Alarm an Telegram senden")
    parser.add_argument("--status-report", action="store_true", help="Live-Statusbericht an Telegram senden")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.demo:
        return run_demo()
    if args.status_report:
        return run_status_report()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
