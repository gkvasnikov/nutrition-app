"""
Собирает рестораны Кройцберга через Google Places Nearby Search.
Bbox: 52.4878, 13.3800, 52.5100, 13.4300
Метод: сетка квадратов 500м × 500м, radius=350м на каждый квадрат.

Использование:
  python3 scrapers/google_places_collector.py
  python3 scrapers/google_places_collector.py --resume
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
import os

load_dotenv()

# ── Константы ──────────────────────────────────────────────────────────────

BBOX = (52.4878, 13.3800, 52.5100, 13.4300)   # min_lat, min_lon, max_lat, max_lon
CELL_SIZE_M = 500
SEARCH_RADIUS_M = 350
PAUSE_S = 0.3
TYPES = ["restaurant", "cafe", "food"]

OUT_FILE = Path("data/kreuzberg/restaurants_kreuzberg.json")
PROGRESS_FILE = Path("data/kreuzberg/.progress.json")

NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

DETAILS_FIELDS = (
    "place_id,name,formatted_address,geometry,"
    "opening_hours,rating,user_ratings_total,"
    "price_level,website,formatted_phone_number"
)

# ── Сетка ──────────────────────────────────────────────────────────────────

# 1 градус широты ≈ 111 000 м
LAT_DEG_PER_M = 1.0 / 111_000
# 1 градус долготы зависит от широты
center_lat = (BBOX[0] + BBOX[2]) / 2
LON_DEG_PER_M = 1.0 / (111_000 * math.cos(math.radians(center_lat)))

LAT_STEP = CELL_SIZE_M * LAT_DEG_PER_M
LON_STEP = CELL_SIZE_M * LON_DEG_PER_M


def build_grid() -> list[tuple[float, float, str]]:
    """Возвращает список (center_lat, center_lon, cell_id) для всех квадратов bbox."""
    cells = []
    lat = BBOX[0] + LAT_STEP / 2
    row = 0
    while lat < BBOX[2]:
        lon = BBOX[1] + LON_STEP / 2
        col = 0
        while lon < BBOX[3]:
            cell_id = f"{row}_{col}"
            cells.append((lat, lon, cell_id))
            lon += LON_STEP
            col += 1
        lat += LAT_STEP
        row += 1
    return cells


# ── HTTP ───────────────────────────────────────────────────────────────────

def _get(url: str, params: dict) -> dict:
    api_key = os.environ["GOOGLE_PLACES_API_KEY"]
    params["key"] = api_key
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


# ── Прогресс ───────────────────────────────────────────────────────────────

def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()))
    return set()


def save_progress(done: set[str]) -> None:
    PROGRESS_FILE.write_text(json.dumps(sorted(done), ensure_ascii=False))


def load_restaurants() -> dict[str, dict]:
    if OUT_FILE.exists():
        data = json.loads(OUT_FILE.read_text())
        return {r["place_id"]: r for r in data}
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
        "kbju_status": "no_data",
        "menu": [],
    }


def in_bbox(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    return BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]


# ── Сбор ───────────────────────────────────────────────────────────────────

def collect_cell(lat: float, lon: float, restaurants: dict[str, dict],
                 new_ids: list[str]) -> int:
    """Делает Nearby Search по всем типам для одного квадрата."""
    found = 0
    candidate_ids: list[str] = []

    for place_type in TYPES:
        page_token = None
        while True:
            data = nearby_search(lat, lon, place_type, page_token)
            time.sleep(PAUSE_S)

            for place in data.get("results", []):
                pid = place["place_id"]
                if pid not in restaurants and pid not in {c for c in candidate_ids}:
                    candidate_ids.append(pid)

            page_token = data.get("next_page_token")
            if not page_token:
                break
            time.sleep(2)   # Google требует паузу перед next_page_token

    for pid in candidate_ids:
        if pid in restaurants:
            continue
        detail = place_details(pid)
        time.sleep(PAUSE_S)

        loc = detail.get("geometry", {}).get("location", {})
        if not in_bbox(loc.get("lat"), loc.get("lng")):
            continue

        entry = format_entry(detail)
        restaurants[pid] = entry
        new_ids.append(pid)
        found += 1
        print(f"      + {entry['name']}")

    return found


def run(resume: bool) -> None:
    grid = build_grid()
    done_cells = load_progress() if resume else set()
    restaurants = load_restaurants() if resume else {}

    total_cells = len(grid)
    rows = max(r for _, _, cid in grid for r in [int(cid.split("_")[0])]) + 1
    cols = max(c for _, _, cid in grid for c in [int(cid.split("_")[1])]) + 1
    print(f"Сетка: {rows}×{cols} = {total_cells} квадратов")
    print(f"Уже обработано: {len(done_cells)} квадратов, "
          f"{len(restaurants)} заведений в БД\n")

    new_ids: list[str] = []
    processed = 0

    for lat, lon, cell_id in grid:
        if cell_id in done_cells:
            continue

        print(f"  [{cell_id}] lat={lat:.4f} lon={lon:.4f} ...", end=" ", flush=True)
        found = collect_cell(lat, lon, restaurants, new_ids)
        print(f"{found} новых")

        done_cells.add(cell_id)
        processed += 1
        save_restaurants(restaurants)
        save_progress(done_cells)

    # Итог
    print(f"\n{'─'*50}")
    print(f"Квадратов обработано : {processed} (из {total_cells})")
    print(f"Уникальных заведений : {len(restaurants)}")
    print(f"Новых за этот запуск : {len(new_ids)}")
    print(f"Файл                 : {OUT_FILE}")

    if done_cells == set(cid for _, _, cid in grid):
        PROGRESS_FILE.unlink(missing_ok=True)
        print("Прогресс-файл удалён (все квадраты обработаны)")


if __name__ == "__main__":
    resume = "--resume" in sys.argv
    run(resume=resume)
