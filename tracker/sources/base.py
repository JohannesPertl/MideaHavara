"""Gemeinsame Infrastruktur für alle Quellen-Adapter."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time

import requests

log = logging.getLogger(__name__)

# Realistische Browser-Header – viele Shops blocken sonst mit HTTP 403.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Zeitbudgets bewusst knapp, damit ein 10-Minuten-Lauf nicht überzogen wird.
DEFAULT_TIMEOUT = 15  # Sekunden pro HTTP-Request
BROWSER_GOTO_MS = 15000  # Seitenaufbau im Browser-Fallback
BROWSER_SELECTOR_MS = 6000  # Warten auf Selektor (kurz – fehlt er, geht's ohne)


def browser_headers(extra: dict | None = None) -> dict:
    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra:
        headers.update(extra)
    return headers


def get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(browser_headers())
    return s


def http_get(
    url: str,
    *,
    session: requests.Session | None = None,
    headers: dict | None = None,
    params: dict | None = None,
    retries: int = 2,
    timeout: int = DEFAULT_TIMEOUT,
) -> requests.Response | None:
    """GET mit Retry/Backoff. Gibt None zurück, wenn dauerhaft fehlschlägt.

    Bei dauerhaftem Block (403/404/451) wird nicht weiter wiederholt – ein
    erneuter Versuch ändert daran nichts und kostet nur Zeit.
    """
    sess = session or get_session()
    for attempt in range(retries):
        try:
            resp = sess.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
            log.warning("GET %s -> HTTP %s (Versuch %d)", url, resp.status_code, attempt + 1)
            if resp.status_code in (403, 404, 451):
                break  # Block/„nicht da“ – Retry zwecklos, direkt zum Fallback
        except requests.RequestException as exc:
            log.warning("GET %s fehlgeschlagen: %s (Versuch %d)", url, exc, attempt + 1)
        if attempt < retries - 1:
            time.sleep(min(2 ** attempt + random.random(), 3))
    return None


def fetch_page(url: str, *, wait_selector: str | None = None) -> tuple[str | None, str]:
    """Holt eine Produktseite robust: direkt, sonst Stealth-Browser.

    Wichtig: Manche Shops (Hornbach/Amazon) liefern eine Bot-Wall mit HTTP 200
    und winzigem HTML. Solche „Erfolge" werden als Challenge erkannt und gehen
    trotzdem in den Browser-Fallback. Returns (html, how) mit how ∈
    {"direct","browser","blocked"}.
    """
    resp = http_get(url)
    if resp is not None and resp.status_code == 200 and not _looks_like_challenge(resp.text):
        return resp.text, "direct"

    html = fetch_html_via_browser(url, wait_selector=wait_selector)
    if html and not _looks_like_challenge(html):
        return html, "browser"

    # Beste verfügbare (evtl. Challenge-)Antwort zurückgeben, klar markiert.
    return (html or (resp.text if resp is not None else None)), "blocked"


def http_get_json(url: str, **kwargs) -> dict | list | None:
    resp = http_get(url, **kwargs)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError:
        log.warning("Antwort von %s ist kein gültiges JSON.", url)
        return None


# JS-Init zum Verschleiern typischer Headless-/Automations-Merkmale.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['de-DE','de','en-US','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
const _q = navigator.permissions && navigator.permissions.query;
if (_q) { navigator.permissions.query = (p) => (
  p && p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _q(p)
); }
"""


def _launch_chromium(pw):
    launch_options = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if executable:
        launch_options["executable_path"] = executable
    return pw.chromium.launch(**launch_options)

# Marker einer Bot-Wall / Challenge-Seite (dann nachladen statt aufgeben).
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "/cdn-cgi/",
    "captcha",
    "are you a human",
    "enable javascript and cookies",
    "verifying you are human",
)


def _looks_like_challenge(html: str | None) -> bool:
    if not html or len(html) < 12000:
        return True
    low = html.lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


def fetch_html_via_browser(
    url: str,
    *,
    wait_selector: str | None = None,
    timeout_ms: int = BROWSER_GOTO_MS,
    stealth: bool = True,
) -> str | None:
    """Fallback für bot-feindliche Seiten: echte Browser-Session via Playwright.

    Mit ``stealth`` (Default) werden gängige Automations-Merkmale verschleiert
    und JS-Challenge-Seiten kurz nachgeladen, um Bot-Walls (Cloudflare/Akamai)
    zu überwinden. Schlägt Import/Start fehl, wird None zurückgegeben.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("Playwright nicht installiert – Browser-Fallback für %s übersprungen.", url)
        return None

    ua = random.choice(_USER_AGENTS)
    try:
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            ctx = browser.new_context(
                user_agent=ua,
                locale="de-DE",
                timezone_id="Europe/Berlin",
                viewport={"width": 1366, "height": 768},
                extra_http_headers={
                    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
                    "sec-ch-ua": '"Chromium";v="124", "Not:A-Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                },
            )
            if stealth:
                ctx.add_init_script(_STEALTH_INIT)

            page = ctx.new_page()
            page.set_default_timeout(BROWSER_SELECTOR_MS)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:  # noqa: BLE001 - langsame/blockierende Seite
                pass
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=BROWSER_SELECTOR_MS)
                except Exception:  # noqa: BLE001 - Selektor optional
                    pass

            html = page.content()

            # Challenge erkannt → kurz auf JS-Auflösung warten und erneut lesen.
            if stealth and _looks_like_challenge(html):
                for _ in range(2):
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(2500)
                    html = page.content()
                    if not _looks_like_challenge(html):
                        break

            browser.close()
            return html
    except Exception as exc:  # noqa: BLE001 - Browser-Fallback ist best effort
        log.warning("Browser-Fallback für %s fehlgeschlagen: %s", url, exc)
        return None


def fetch_json_via_browser(
    url: str,
    *,
    referer: str | None = None,
    headers: dict | None = None,
    timeout_ms: int = BROWSER_GOTO_MS,
) -> dict | list | None:
    """Holt eine JSON-API über eine echte Browser-Session (gegen Bot-Walls).

    Manche Endpoints (z.B. die MediaMarkt/Saturn-Filial-API) liefern an
    Rechenzentrums-IPs eine HTTP-403-Bot-Wall, sobald man sie mit ``requests``
    aufruft. Wir besuchen daher zuerst die ``referer``-Seite (löst die
    JS-Challenge, setzt Cookies) und rufen die API dann per In-Page ``fetch()``
    auf – mit dem echten Browser-Networking und den frisch gesetzten Cookies.

    Gibt das geparste JSON zurück oder ``None`` (Import/Start fehlgeschlagen,
    geblockt oder kein gültiges JSON) – immer best effort, nie werfend.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("Playwright nicht installiert – JSON-Browser-Fallback für %s übersprungen.", url)
        return None

    ua = random.choice(_USER_AGENTS)
    try:
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            ctx = browser.new_context(
                user_agent=ua,
                locale="de-DE",
                timezone_id="Europe/Berlin",
                viewport={"width": 1366, "height": 768},
            )
            ctx.add_init_script(_STEALTH_INIT)
            page = ctx.new_page()
            page.set_default_timeout(BROWSER_SELECTOR_MS)

            # 1) Referer-Seite besuchen, um Challenge zu lösen / Cookies zu setzen.
            if referer:
                try:
                    page.goto(referer, wait_until="domcontentloaded", timeout=timeout_ms)
                    if _looks_like_challenge(page.content()):
                        page.wait_for_timeout(2500)
                except Exception:  # noqa: BLE001 - langsame/blockierende Seite
                    pass

            # 2) API per In-Page-fetch() mit echten Browser-Cookies abrufen.
            try:
                result = page.evaluate(
                    """async ({url, headers}) => {
                        try {
                            const r = await fetch(url, {headers, credentials: 'include'});
                            return {status: r.status, body: await r.text()};
                        } catch (e) { return {status: 0, body: ''}; }
                    }""",
                    {"url": url, "headers": headers or {}},
                )
            finally:
                browser.close()

        if not result or result.get("status") != 200:
            log.info("JSON-Browser-Fallback %s -> HTTP %s", url, result and result.get("status"))
            return None
        try:
            return json.loads(result["body"])
        except (ValueError, TypeError):
            log.warning("JSON-Browser-Fallback: Antwort von %s ist kein gültiges JSON.", url)
            return None
    except Exception as exc:  # noqa: BLE001 - best effort
        log.warning("JSON-Browser-Fallback für %s fehlgeschlagen: %s", url, exc)
        return None


_PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*|\d+)(?:[,.](\d{2}))?")


def parse_price(text: str) -> float | None:
    """Extrahiert einen EUR-Preis aus deutschem Text (z.B. '1.299,00 €')."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ")
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None
    whole = m.group(1).replace(".", "").replace(" ", "")
    cents = m.group(2) or "00"
    try:
        return float(f"{whole}.{cents}")
    except ValueError:
        return None
