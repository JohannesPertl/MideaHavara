"""Discover MediaMarkt stores around configured areas."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

from .config import CONFIG_PATH, STORES_PATH
from .matching import haversine_km

log = logging.getLogger(__name__)

USER_AGENT = "MideaHavara/1.0 (private availability tracker)"
STORE_FINDER_URL = "https://www.mediamarkt.at/de/store/store-finder"
STORE_URL = "https://www.mediamarkt.at/de/store/{uid}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


@dataclass(frozen=True)
class Area:
    name: str
    latitude: float
    longitude: float
    radius_km: float


@dataclass(frozen=True)
class DiscoveredStore:
    id: str
    name: str
    lat: float
    lon: float
    distance_km: float


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _save_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _hydration_json(html: str) -> dict | None:
    match = re.search(r'window\.__staticRouterHydrationData = JSON\.parse\("(.*?)"\);</script>', html, re.S)
    if not match:
        return None
    try:
        return json.loads(json.loads('"' + match.group(1) + '"'))
    except (json.JSONDecodeError, TypeError):
        return None


def _local_store_page(data: dict) -> dict:
    return (((data.get("loaderData") or {}).get("0-17") or {}).get("data") or {}).get("localStorePage") or {}


def geocode_area(query: str) -> Area:
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "at"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Ort nicht gefunden: {query}")
    hit = data[0]
    return Area(
        name=hit.get("display_name") or query,
        latitude=float(hit["lat"]),
        longitude=float(hit["lon"]),
        radius_km=0,
    )


def store_finder_entries(session: requests.Session | None = None) -> list[dict]:
    sess = session or requests.Session()
    resp = sess.get(STORE_FINDER_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    data = _hydration_json(resp.text)
    if not data:
        raise RuntimeError("MediaMarkt store finder did not expose route data")

    page = _local_store_page(data)
    entries: list[dict] = []
    for block in page.get("content") or []:
        for region in block.get("regions") or []:
            for store in region.get("regionStores") or []:
                uid = store.get("uid")
                name = (store.get("storeName") or "").strip()
                if uid and name:
                    entries.append({"uid": uid, "name": name})
    return entries


def _jsonld_geo(html: str) -> tuple[float, float] | None:
    match = re.search(r'"geo"\s*:\s*\{[^}]*"latitude"\s*:\s*([\d.]+)\s*,\s*"longitude"\s*:\s*([\d.]+)', html)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def store_detail(uid: str, fallback_name: str, session: requests.Session | None = None) -> dict | None:
    sess = session or requests.Session()
    resp = sess.get(STORE_URL.format(uid=uid), headers={"User-Agent": USER_AGENT}, timeout=30)
    if resp.status_code not in (200, 301, 302):
        log.info("MediaMarkt store %s returned HTTP %s", uid, resp.status_code)
        return None
    if resp.status_code in (301, 302):
        resp = sess.get(resp.headers["Location"], headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()

    data = _hydration_json(resp.text) or {}
    page = _local_store_page(data)
    store_id = str(page.get("storeId") or "").strip()
    name = (page.get("storeName") or fallback_name).strip()
    geo = _jsonld_geo(resp.text)
    if not store_id or not geo:
        log.info("MediaMarkt store %s missing id or coordinates", uid)
        return None
    return {"id": store_id, "name": name, "lat": geo[0], "lon": geo[1]}


def discover_mediamarkt_stores(areas: list[Area]) -> list[DiscoveredStore]:
    if not areas:
        return []

    sess = requests.Session()
    entries = store_finder_entries(sess)
    found: dict[str, DiscoveredStore] = {}
    for entry in entries:
        detail = store_detail(entry["uid"], entry["name"], sess)
        if not detail:
            continue
        nearest = min(
            haversine_km(area.latitude, area.longitude, detail["lat"], detail["lon"])
            for area in areas
        )
        if not any(nearest <= area.radius_km for area in areas):
            continue
        found[detail["id"]] = DiscoveredStore(
            id=detail["id"],
            name=detail["name"],
            lat=detail["lat"],
            lon=detail["lon"],
            distance_km=nearest,
        )
    return sorted(found.values(), key=lambda s: (s.distance_km, s.name))


def config_areas(config_path: Path = CONFIG_PATH) -> list[Area]:
    data = _load_yaml(config_path)
    if "areas" in data and not data.get("areas"):
        return []
    raw_areas = data.get("areas") or []
    areas: list[Area] = []
    for item in raw_areas:
        if not isinstance(item, dict):
            continue
        try:
            areas.append(
                Area(
                    name=str(item["name"]),
                    latitude=float(item["latitude"]),
                    longitude=float(item["longitude"]),
                    radius_km=float(item["radius_km"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if areas:
        return areas

    loc = data.get("location") or {}
    try:
        return [
            Area(
                name=f"{loc.get('city', 'Location')} {loc.get('postal_code', '')}".strip(),
                latitude=float(loc["latitude"]),
                longitude=float(loc["longitude"]),
                radius_km=float(loc["radius_km"]),
            )
        ]
    except (KeyError, TypeError, ValueError):
        return []


def save_areas_and_stores(
    areas: list[Area],
    stores: list[DiscoveredStore],
    *,
    config_path: Path = CONFIG_PATH,
    stores_path: Path = STORES_PATH,
) -> None:
    cfg = _load_yaml(config_path)
    cfg["areas"] = [
        {
            "name": area.name,
            "latitude": area.latitude,
            "longitude": area.longitude,
            "radius_km": area.radius_km,
        }
        for area in areas
    ]
    if areas:
        primary = areas[0]
        location = dict(cfg.get("location") or {})
        location.update(
            {
                "postal_code": location.get("postal_code", ""),
                "city": primary.name.split(",", 1)[0],
                "latitude": primary.latitude,
                "longitude": primary.longitude,
                "radius_km": max(area.radius_km for area in areas),
            }
        )
        cfg["location"] = location
    _save_yaml(config_path, cfg)

    store_data = _load_yaml(stores_path)
    store_data["mediamarkt"] = [
        {
            "id": store.id,
            "name": store.name,
            "lat": store.lat,
            "lon": store.lon,
            "distance_km": round(store.distance_km, 1),
        }
        for store in stores
    ]
    for chain in ("saturn", "obi", "bauhaus", "hornbach"):
        store_data.setdefault(chain, [])
    _save_yaml(stores_path, store_data)


def add_area_and_refresh(name: str, radius_km: float) -> tuple[Area, list[DiscoveredStore]]:
    geocoded = geocode_area(name)
    new_area = Area(geocoded.name, geocoded.latitude, geocoded.longitude, radius_km)
    areas = [area for area in config_areas() if area.name != new_area.name]
    areas.append(new_area)
    stores = discover_mediamarkt_stores(areas)
    save_areas_and_stores(areas, stores)
    return new_area, stores


def clear_areas_and_refresh() -> None:
    save_areas_and_stores([], [])
