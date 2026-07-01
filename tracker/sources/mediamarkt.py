"""MediaMarkt-/Saturn-Adapter (gemeinsamer Code für beide Ketten).

Strategie:
  1. Online-Preis & -Verfügbarkeit robust aus der Produktseite (JSON-LD).
  2. Filial-Bestand (best effort) über das inoffizielle GraphQL-Endpoint
     ``GetProductAvailabilities`` für die in stores.yaml hinterlegten Märkte.

Hinweis: Das GraphQL-Endpoint nutzt rotierende "persisted query"-Hashes und
hat Anti-Bot-Schutz. Es kann zeitweise HTML statt JSON liefern; in dem Fall
fällt der Adapter still auf die Online-Daten zurück.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode, urlparse

from ..config import Config, Product, Store
from ..models import CHANNEL_ONLINE, CHANNEL_STORE, CONDITION_NEW, Offer
from .base import browser_headers, fetch_json_via_browser, fetch_page, http_get_json
from .buyability import assess_buyability
from .embedded_json import extract_offers_for_product
from .jsonld import extract_products

log = logging.getLogger(__name__)

_DOMAINS = {
    "mediamarkt": {
        "DE": "www.mediamarkt.de",
        "AT": "www.mediamarkt.at",
    },
    "saturn": {
        "DE": "www.saturn.de",
    },
}
_SALESLINE = {"mediamarkt": "Media", "saturn": "Saturn"}

# Persisted-Query-Hash der GetProductAvailabilities-Operation.
#
# WICHTIG: Dieser Hash rotiert mit jedem PWA-Release (aktuell ~v8.451.0). Ist er
# veraltet, antwortet die API mit HTTP 200 + "PersistedQueryNotFound" und es
# kommt kein Filialbestand (sauber, kein Fehlalarm). Aktualisieren:
#   1. Produktseite im Browser öffnen, DevTools → Netzwerk, Filter "graphql".
#   2. Unten "Verfügbarkeit im Markt" prüfen (PLZ eingeben) → der dann gefeuerte
#      Request "GetProductAvailabilities" enthält in der URL den aktuellen
#      sha256Hash. Diesen hier eintragen.
_AVAIL_QUERY_HASH = "810286f7ae9368cb54ccd122b21e453bd00ed7dbf534b8259b8bed68f8da999f"

# Header, die die echte PWA an /api/v1/graphql sendet. Ohne den korrekten
# client-name ("pwa-client-pqm", NICHT "pwa") blockt die WAF mit HTTP 403.
_GQL_CLIENT_NAME = "pwa-client-pqm"
_GQL_CLIENT_VERSION = "8.451.0"

_IN_STORE_TOKENS = {"IN_STORE", "IN_WAREHOUSE", "AVAILABLE", "PICKUP"}
# Negativ-Status, die "AVAILABLE" als Teilstring enthalten (z.B. NOT_AVAILABLE,
# UNAVAILABLE) und sonst fälschlich als verfügbar zählen würden.
_NOT_IN_STORE_MARKERS = ("NOT_AVAILABLE", "UNAVAILABLE", "NOT AVAILABLE", "OUT_OF_STOCK", "SOLD")


def _status_in_store(status: str) -> bool:
    """Ob ein Filial-Status echten Bestand bedeutet (Negativ-Status haben Vorrang)."""
    status = status.upper()
    if any(neg in status for neg in _NOT_IN_STORE_MARKERS):
        return False
    return any(tok in status for tok in _IN_STORE_TOKENS)


def _product_html(url: str) -> str | None:
    # fetch_page erkennt auch HTTP-200-Bot-Walls und geht dann in den
    # Stealth-Browser-Fallback (statt eine Challenge-Seite zu parsen).
    html, how = fetch_page(url, wait_selector="script[type='application/ld+json']")
    if how == "blocked":
        log.info("MediaMarkt/Saturn: Bot-Wall nicht überwunden (geblockt).")
    return html


def _online_offers(cfg: Config, product: Product, chain: str, url: str) -> list[Offer]:
    html = _product_html(url)
    if not html:
        return []
    product_ean = product.eans[0] if product.eans else None
    offers: list[Offer] = []

    # 1) Echtes schema.org (falls vorhanden).
    products = extract_products(html)
    for prod in products:
        if prod["price"] is None:
            continue
        buyable, signals = assess_buyability(html, jsonld_in_stock=prod["in_stock"])
        log.info(
            "%s online (ld+json): availability=%s -> bestellbar=%s %s",
            chain, prod.get("availability_raw"), buyable, signals,
        )
        offers.append(
            Offer(
                source=chain,
                title=prod["title"] or product.name,
                price=prod["price"],
                url=url,
                in_stock=buyable,
                condition=CONDITION_NEW,
                channel=CHANNEL_ONLINE,
                ean=prod["ean"] or product_ean,
                merchant=chain.capitalize(),
            )
        )

    # 2) Fallback: eingebettetes JSON, an die Produkt-ID gekoppelt (SPA-Plattform).
    if not products:
        product_id = _extract_product_id(url)
        for emb in extract_offers_for_product(html, product_id or ""):
            # Negativ-Marker der Seite (z.B. "ausverkauft") als zusätzliches Veto.
            page_ok, _ = assess_buyability(html, jsonld_in_stock=emb["in_stock"])
            buyable = emb["in_stock"] and page_ok
            log.info(
                "%s online (embedded): price=%s availability=%s -> bestellbar=%s",
                chain, emb["price"], emb["availability_raw"], buyable,
            )
            offers.append(
                Offer(
                    source=chain,
                    title=product.name,
                    price=emb["price"],
                    url=url,
                    in_stock=buyable,
                    condition=CONDITION_NEW,
                    channel=CHANNEL_ONLINE,
                    ean=product_ean,
                    merchant=chain.capitalize(),
                )
            )
    return offers


def _extract_product_id(url: str) -> str | None:
    """Item-Nummer aus einer MediaMarkt/Saturn-Produkt-URL (…-<id>.html)."""
    tail = url.rstrip("/").rsplit("-", 1)[-1]
    digits = "".join(ch for ch in tail if ch.isdigit())
    return digits or None


def _market_context(chain: str, url: str) -> tuple[str, str, str]:
    """Return GraphQL domain, country, and language for the product URL."""
    host = urlparse(url).netloc.lower()
    if host.endswith("mediamarkt.at"):
        return _DOMAINS["mediamarkt"]["AT"], "AT", "de"
    if host.endswith("mediamarkt.de"):
        return _DOMAINS["mediamarkt"]["DE"], "DE", "de"
    if host.endswith("saturn.de"):
        return _DOMAINS["saturn"]["DE"], "DE", "de"

    domain = _DOMAINS.get(chain, {}).get("DE", "www.mediamarkt.de")
    return domain, "DE", "de"


def _store_offers(
    cfg: Config, product: Product, chain: str, url: str, online_price: float | None
) -> list[Offer]:
    stores = [s for s in cfg.stores_for(chain) if s.id]  # nur mit bekannter Store-ID
    product_id = _extract_product_id(url)
    if not stores or not product_id:
        return []

    domain, country, language = _market_context(chain, url)
    endpoint = f"https://{domain}/api/v1/graphql"
    params = {
        "operationName": "GetProductAvailabilities",
        "variables": f'{{"ids":["{product_id}"]}}',
        "extensions": (
            '{"pwa":{"salesLine":"%s","country":"%s","language":"%s"},'
            '"persistedQuery":{"version":1,"sha256Hash":"%s"}}'
            % (_SALESLINE[chain], country, language, _AVAIL_QUERY_HASH)
        ),
    }
    headers = browser_headers({
        "Accept": "*/*",
        "Content-Type": "application/json",
        "apollographql-client-name": _GQL_CLIENT_NAME,
        "apollographql-client-version": _GQL_CLIENT_VERSION,
        "x-operation": "GetProductAvailabilities",
        "x-cacheable": "true",
        "x-flow-id": str(uuid.uuid4()),
    })

    # 1) Direkt versuchen (schnell). 2) Bei Bot-Wall (403/HTML) über eine echte
    #    Browser-Session, die zuerst die Produktseite besucht (Cookies/Challenge)
    #    und die API dann per In-Page-fetch() abruft.
    data = http_get_json(endpoint, headers=headers, params=params)
    if not isinstance(data, dict):
        full_url = f"{endpoint}?{urlencode(params)}"
        data = fetch_json_via_browser(full_url, referer=url, headers=headers)
    if not isinstance(data, dict):
        log.info("%s: Filial-API lieferte kein JSON – Filialbestand übersprungen.", chain)
        return []

    avails = (((data.get("data") or {}).get("availabilities")) or [])
    if not isinstance(avails, list):
        return []

    by_store: dict[str, dict] = {}
    for entry in avails:
        if isinstance(entry, dict):
            sid = str(entry.get("storeId") or entry.get("id") or "")
            if sid:
                by_store[sid] = entry

    offers: list[Offer] = []
    for store in stores:
        if not store.id:
            # Filiale ist hinterlegt, aber die ketteninterne ID fehlt noch
            # (siehe stores.yaml) – ohne ID keine Bestandsabfrage möglich.
            log.info("%s: Filiale '%s' ohne Store-ID – übersprungen.", chain, store.name)
            continue
        entry = by_store.get(store.id)
        if not entry:
            continue
        status = str(entry.get("availabilityType") or entry.get("status") or "")
        if not _status_in_store(status):
            continue
        offers.append(
            Offer(
                source=chain,
                title=product.name,
                price=online_price or product.max_price,
                url=url,
                in_stock=True,
                condition=CONDITION_NEW,
                channel=CHANNEL_STORE,
                ean=product.eans[0] if product.eans else None,
                merchant=chain.capitalize(),
                store_name=store.name,
                distance_km=_distance(cfg, store),
            )
        )
    return offers


def _distance(cfg: Config, store: Store) -> float | None:
    if store.distance_km is not None:
        return store.distance_km
    if store.lat is None or store.lon is None:
        return None
    from ..matching import haversine_km

    return haversine_km(cfg.location.latitude, cfg.location.longitude, store.lat, store.lon)


def fetch_offers(cfg: Config, product: Product, chain: str = "mediamarkt") -> list[Offer]:
    url = product.url_for(chain)
    if not url:
        log.info("%s: keine Produkt-URL für '%s' konfiguriert – übersprungen.", chain, product.name)
        return []

    online = _online_offers(cfg, product, chain, url)
    online_price = online[0].price if online else None
    store = _store_offers(cfg, product, chain, url, online_price)

    log.info("%s: %d online + %d Filial-Angebote.", chain, len(online), len(store))
    return online + store
