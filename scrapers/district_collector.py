#!/usr/bin/env python3
"""
Generic district restaurant collector via Google Places Nearby Search.

Bbox Kreuzberg (updated):
  (52.460, 13.400, 52.513, 13.460)

Usage:
  # Full bbox scan:
  python3 scrapers/district_collector.py

  # Incremental — only the new northern strip (lat 52.490 → 52.513),
  # deduplicates against data/all_restaurants.json,
  # saves only new restaurants to data/kreuzberg_north_patch/restaurants.json:
  python3 scrapers/district_collector.py --incremental
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


def _find_env() -> Path:
    c = Path(__file__).parent.parent
    for _ in range(6):
        if (c / ".env").exists():
            return c / ".env"
        c = c.parent
    return Path(__file__).parent.parent / ".env"


load_dotenv(_find_env())

ROOT = Path(__file__).parent.parent

# ── Bbox ───────────────────────────────────────────────────────────────────

BBOX_FULL        = (52.460, 13.400, 52.513, 13.460)   # полный Kreuzberg
BBOX_INCREMENTAL = (52.490, 13.400, 52.513, 13.460)   # только новая северная полоса

CELL_SIZE_M    = 500
SEARCH_RADIUS_M = 350
PAUSE_S        = 0.3

SEARCH_TYPES = ["restaurant", "cafe", "meal_takeaway"]

KEEP_TYPES = {
    "restaurant", "cafe", "meal_takeaway",
    "meal_delivery", "bakery", "food",
}
DROP_TYPES = {
    "bar", "night_club", "liquor_store",
    "casino", "lodging",
}
DROP_NAME_RE = re.compile(
    r"\bbar\b|pub|kneipe|lounge|club|disco|cocktail|shots",
    re.IGNORECASE,
)

NEARBY_URL  = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

DETAILS_FIELDS = (
    "place_id,name,formatted_address,geometry,"
    "opening_hours,rating,user_ratings_total,"
    "price_level,website,formatted_phone_number"
)

ALL_RESTAURANTS_FILE = ROOT / "data" / "all_restaurants.json"
PATCH_DIR  = ROOT / "data" / "kreuzberg_north_patch"
PATCH_FILE = PATCH_DIR / "restaurants.json"


# ── Сетка ──────────────────────────────────────────────────────────────────

def build_grid(bbox: tuple) -> list[tuple[float, float, str]]:
    min_lat, min_lon, max_lat, max_lon = bbox
    center_lat = (min_lat + max_lat) / 2
    lat_step = CELL_SIZE_M / 111_000
    lon_step = CELL_SIZE_M / (111_000 * math.cos(math.radians(center_lat)))

    cells = []
    row, lat = 0, min_lat + lat_step / 2
    while lat < max_lat:
        col, lon = 0, min_lon + lon_step / 2
        while lon < max_lon:
            cells.append((lat, lon, f"{row}_{col}"))
            lon += lon_step
            col += 1
        lat += lat_step
        row += 1
    return cells


# ── HTTP ───────────────────────────────────────────────────────────────────

def _get(url: str, params: dict) -> dict:
    params["key"] = os.environ["GOOGLE_PLACES_API_KEY"]
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def nearby_search(lat: float, lon: float, place_type: str,
                  page_token: str | None = None) -> dict:
    if page_token:
        return _get(NEARBY_URL, {"pagetoken": page_token})
    return _get(NEARBY_URL, {
        "location": f"{lat},{lon}",
        "radius": SEARCH_RADIUS_M,
        "type": place_type,
    })


def place_details(place_id: str) -> dict:
    data = _get(DETAILS_URL, {"place_id": place_id, "fields": DETAILS_FIELDS})
    return data.get("result", {})


# ── Фильтрация ─────────────────────────────────────────────────────────────

def should_keep(place: dict) -> bool:
    types = set(place.get("types", []))
    name  = place.get("name", "")
    if types & DROP_TYPES:            return False
    if DROP_NAME_RE.search(name):     return False
    if not (types & KEEP_TYPES):      return False
    return True


def in_bbox(lat, lon, bbox: tuple) -> bool:
    if lat is None or lon is None:
        return False
    return bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]


# ── Дедупликация ───────────────────────────────────────────────────────────

def load_existing_ids() -> set[str]:
    """Возвращает все google_place_id / place_id из all_restaurants.json."""
    if not ALL_RESTAURANTS_FILE.exists():
        return set()
    data = json.loads(ALL_RESTAURANTS_FILE.read_text())
    ids: set[str] = set()
    for r in data:
        for field in ("google_place_id", "place_id"):
            v = r.get(field)
            if v:
                ids.add(v)
    return ids


# ── Формат записи ──────────────────────────────────────────────────────────

def format_entry(detail: dict) -> dict:
    loc = detail.get("geometry", {}).get("location", {})
    return {
        "place_id":      detail.get("place_id", ""),
        "name":          detail.get("name", ""),
        "address":       detail.get("formatted_address", ""),
        "lat":           loc.get("lat"),
        "lon":           loc.get("lng"),
        "opening_hours": detail.get("opening_hours", {}),
        "rating":        detail.get("rating"),
        "reviews_count": detail.get("user_ratings_total"),
        "price_level":   detail.get("price_level"),
        "website":       detail.get("website", ""),
        "phone":         detail.get("formatted_phone_number", ""),
        "district":      "kreuzberg_north",
        "kbju_status":   "no_data",
        "wolt_menu":     [],
        "site_menu":     [],
    }


def save(restaurants: list, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    tmp.replace(path)


# ── Сбор ───────────────────────────────────────────────────────────────────

def collect_cell(lat: float, lon: float, bbox: tuple,
                 seen_pids: set[str]) -> tuple[list[dict], int, int]:
    """Возвращает (новые_записи, найдено_всего, дублей)."""
    candidates: dict[str, dict] = {}

    for place_type in SEARCH_TYPES:
        page_token = None
        while True:
            data = nearby_search(lat, lon, place_type, page_token)
            time.sleep(PAUSE_S)
            for place in data.get("results", []):
                pid = place["place_id"]
                if pid not in candidates:
                    candidates[pid] = place
            page_token = data.get("next_page_token")
            if not page_token:
                break
            time.sleep(2)

    new_entries: list[dict] = []
    total = len(candidates)
    dupes = 0

    for pid, place in candidates.items():
        if not should_keep(place):
            total -= 1   # не считаем бары как «найденных ресторанов»
            continue
        if pid in seen_pids:
            dupes += 1
            continue

        detail = place_details(pid)
        time.sleep(PAUSE_S)

        loc = detail.get("geometry", {}).get("location", {})
        if not in_bbox(loc.get("lat"), loc.get("lng"), bbox):
            total -= 1
            continue

        entry = format_entry(detail)
        seen_pids.add(pid)
        new_entries.append(entry)
        print(f"      + {entry['name']}")

    return new_entries, len(new_entries) + dupes, dupes


# ── Main ───────────────────────────────────────────────────────────────────

def run(incremental: bool) -> None:
    if not os.environ.get("GOOGLE_PLACES_API_KEY"):
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set")

    bbox  = BBOX_INCREMENTAL if incremental else BBOX_FULL
    grid  = build_grid(bbox)

    rows = max(int(c.split("_")[0]) for _, _, c in grid) + 1
    cols = max(int(c.split("_")[1]) for _, _, c in grid) + 1

    mode_label = "incremental (52.490 → 52.513)" if incremental else "full"
    print(f"Режим   : {mode_label}")
    print(f"Bbox    : lat {bbox[0]}–{bbox[2]}, lon {bbox[1]}–{bbox[3]}")
    print(f"Сетка   : {rows}×{cols} = {len(grid)} квадратов")

    # Загружаем существующие place_id для дедупликации
    existing_ids = load_existing_ids()
    print(f"Уже в БД: {len(existing_ids)} place_id (из all_restaurants.json)\n")

    seen_pids = set(existing_ids)   # будем добавлять новые по ходу
    all_new: list[dict] = []
    grand_found = grand_new = grand_dupes = 0

    for lat, lon, cell_id in grid:
        print(f"  [{cell_id}] lat={lat:.4f} lon={lon:.4f} ...", end=" ", flush=True)
        new_entries, found, dupes = collect_cell(lat, lon, bbox, seen_pids)
        print(f"найдено {found}, новых {len(new_entries)}, дублей {dupes}")

        all_new.extend(new_entries)
        grand_found += found
        grand_new   += len(new_entries)
        grand_dupes += dupes

        # Checkpoint каждые 5 квадратов
        if len(all_new) and (grid.index((lat, lon, cell_id)) + 1) % 5 == 0:
            PATCH_DIR.mkdir(parents=True, exist_ok=True)
            save(all_new, PATCH_FILE)

    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    save(all_new, PATCH_FILE)

    print(f"\n{'─' * 50}")
    print(f"Найдено всего : {grand_found}")
    print(f"Новых         : {grand_new}")
    print(f"Дублей        : {grand_dupes}")
    print(f"Файл          : {PATCH_FILE}")


if __name__ == "__main__":
    run(incremental="--incremental" in sys.argv)
