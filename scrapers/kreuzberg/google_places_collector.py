"""
Собирает рестораны Кройцберга через Google Places Nearby Search.
Bbox: 52.4878, 13.3800, 52.5100, 13.4300
Метод: сетка 500м × 500м, три запроса на квадрат (restaurant / cafe / meal_takeaway).

Использование:
  python3 scrapers/kreuzberg/google_places_collector.py
  python3 scrapers/kreuzberg/google_places_collector.py --resume
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

load_dotenv()

# ── Константы ──────────────────────────────────────────────────────────────

BBOX = (52.4878, 13.3800, 52.5100, 13.4300)  # min_lat, min_lon, max_lat, max_lon
CELL_SIZE_M = 500
SEARCH_RADIUS_M = 350
PAUSE_S = 0.3

SEARCH_TYPES = ["restaurant", "cafe", "meal_takeaway"]

KEEP_TYPES = {
    "restaurant", "cafe", "meal_takeaway",
    "meal_delivery", "bakery", "food",
}
DROP_TYPES = {
    "bar", "night_club", "liquor_store",
    "casino", "lodging",
}
DROP_NAME_PATTERNS = re.compile(
    r"\bbar\b|pub|kneipe|lounge|club|disco|cocktail|shots",
    re.IGNORECASE,
)

OUT_FILE = Path("data/kreuzberg/restaurants_kreuzberg.json")
PROGRESS_FILE = Path("data/kreuzberg/progress.json")

NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

DETAILS_FIELDS = (
    "place_id,name,formatted_address,geometry,"
    "opening_hours,rating,user_ratings_total,"
    "price_level,website,formatted_phone_number"
)

# ── Сетка ──────────────────────────────────────────────────────────────────

_center_lat = (BBOX[0] + BBOX[2]) / 2
_LAT_STEP = CELL_SIZE_M / 111_000
_LON_STEP = CELL_SIZE_M / (111_000 * math.cos(math.radians(_center_lat)))


def build_grid() -> list[tuple[float, float, str]]:
    cells = []
    row, lat = 0, BBOX[0] + _LAT_STEP / 2
    while lat < BBOX[2]:
        col, lon = 0, BBOX[1] + _LON_STEP / 2
        while lon < BBOX[3]:
            cells.append((lat, lon, f"{row}_{col}"))
            lon += _LON_STEP
            col += 1
        lat += _LAT_STEP
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
    name = place.get("name", "")

    if types & DROP_TYPES:
        return False
    if DROP_NAME_PATTERNS.search(name):
        return False
    if not (types & KEEP_TYPES):
        return False
    return True


# ── Прогресс ───────────────────────────────────────────────────────────────

def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()))
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps(sorted(done), ensure_ascii=False))


def load_restaurants() -> dict[str, dict]:
    if OUT_FILE.exists():
        return {r["place_id"]: r for r in json.loads(OUT_FILE.read_text())}
    return {}


def save_restaurants(restaurants: dict[str, dict]) -> None:
    OUT_FILE.write_text(
        json.dumps(list(restaurants.values()), ensure_ascii=False, indent=2)
    )


# ── Формат записи ──────────────────────────────────────────────────────────

def format_entry(detail: dict) -> dict:
    loc = detail.get("geometry", {}).get("location", {})
    return {
        "place_id": detail.get("place_id", ""),
        "name": detail.get("name", ""),
        "address": detail.get("formatted_address", ""),
        "lat": loc.get("lat"),
        "lon": loc.get("lng"),
        "opening_hours": detail.get("opening_hours", {}),
        "rating": detail.get("rating"),
        "reviews_count": detail.get("user_ratings_total"),
        "price_level": detail.get("price_level"),
        "website": detail.get("website", ""),
        "phone": detail.get("formatted_phone_number", ""),
        "district": "kreuzberg",
        "kbju_status": "no_data",
        "menu": [],
    }


def in_bbox(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]


# ── Сбор ───────────────────────────────────────────────────────────────────

def collect_cell(lat: float, lon: float,
                 restaurants: dict[str, dict]) -> tuple[int, int]:
    """Возвращает (добавлено, отфильтровано) для одного квадрата."""
    candidates: dict[str, dict] = {}  # place_id → raw place

    for place_type in SEARCH_TYPES:
        page_token = None
        while True:
            data = nearby_search(lat, lon, place_type, page_token)
            time.sleep(PAUSE_S)

            for place in data.get("results", []):
                pid = place["place_id"]
                if pid not in candidates and pid not in restaurants:
                    candidates[pid] = place

            page_token = data.get("next_page_token")
            if not page_token:
                break
            time.sleep(2)  # Google требует паузу перед next_page_token

    added = 0
    filtered = 0

    for pid, place in candidates.items():
        if not should_keep(place):
            filtered += 1
            continue

        detail = place_details(pid)
        time.sleep(PAUSE_S)

        loc = detail.get("geometry", {}).get("location", {})
        if not in_bbox(loc.get("lat"), loc.get("lng")):
            filtered += 1
            continue

        entry = format_entry(detail)
        restaurants[pid] = entry
        added += 1
        print(f"      + {entry['name']}")

    return added, filtered


# ── Main ───────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    grid = build_grid()
    done_cells = load_progress() if resume else set()
    restaurants = load_restaurants() if resume else {}

    rows = max(int(cid.split("_")[0]) for _, _, cid in grid) + 1
    cols = max(int(cid.split("_")[1]) for _, _, cid in grid) + 1
    total_cells = len(grid)

    print(f"Сетка: {rows}×{cols} = {total_cells} квадратов")
    print(f"Уже обработано: {len(done_cells)} квадратов, "
          f"{len(restaurants)} заведений в БД\n")

    processed = 0
    total_added = 0
    total_filtered = 0

    for lat, lon, cell_id in grid:
        if cell_id in done_cells:
            continue

        print(f"  [{cell_id}] lat={lat:.4f} lon={lon:.4f} ...", end=" ", flush=True)
        added, filtered = collect_cell(lat, lon, restaurants)
        print(f"{added} новых, {filtered} отфильтровано")

        total_added += added
        total_filtered += filtered
        processed += 1
        done_cells.add(cell_id)

        save_restaurants(restaurants)
        save_progress(done_cells)

    print(f"\n{'─' * 50}")
    print(f"Квадратов обработано : {processed} из {total_cells}")
    print(f"Уникальных заведений : {len(restaurants)}")
    print(f"Отфильтровано        : {total_filtered} баров/клубов/прочего")
    print(f"Файл                 : {OUT_FILE}")

    if done_cells >= {cid for _, _, cid in grid}:
        PROGRESS_FILE.unlink(missing_ok=True)
        print("Прогресс-файл удалён (все квадраты обработаны)")


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
