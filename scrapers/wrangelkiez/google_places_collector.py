"""
Собирает рестораны Вранглькица через Google Places Nearby Search.
Bbox: 52.490, 13.424, 52.505, 13.460
Метод: сетка 400м × 400м, три запроса на квадрат (restaurant / cafe / meal_takeaway).
Сразу собирает photo_url через Places Details API.

Использование:
  python3 scrapers/wrangelkiez/google_places_collector.py
  python3 scrapers/wrangelkiez/google_places_collector.py --resume
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent.parent

BBOX = (52.490, 13.424, 52.505, 13.460)
CELL_SIZE_M = 400
SEARCH_RADIUS_M = 300
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

OUT_FILE = ROOT / "data" / "wrangelkiez" / "restaurants_wrangelkiez.json"
PROGRESS_FILE = ROOT / "data" / "wrangelkiez" / "progress.json"

NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PHOTO_URL   = "https://maps.googleapis.com/maps/api/place/photo"

DETAILS_FIELDS = (
    "place_id,name,formatted_address,geometry,"
    "opening_hours,rating,user_ratings_total,"
    "price_level,website,formatted_phone_number,photos"
)

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


def _get(url: str, params: dict) -> dict:
    params["key"] = os.environ["GOOGLE_PLACES_API_KEY"]
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def nearby_search(lat: float, lon: float, place_type: str,
                  page_token: Optional[str] = None) -> dict:
    if page_token:
        return _get(NEARBY_URL, {"pagetoken": page_token})
    return _get(NEARBY_URL, {
        "location": f"{lat},{lon}",
        "radius": SEARCH_RADIUS_M,
        "type": place_type,
    })


def place_details(place_id: str) -> dict:
    return _get(DETAILS_URL, {
        "place_id": place_id,
        "fields": DETAILS_FIELDS,
        "language": "de",
    })


def make_photo_url(photo_reference: str, max_width: int = 800) -> str:
    key = os.environ["GOOGLE_PLACES_API_KEY"]
    return (
        f"{PHOTO_URL}?maxwidth={max_width}"
        f"&photo_reference={photo_reference}"
        f"&key={key}"
    )


def is_food_place(place: dict) -> bool:
    types = set(place.get("types", []))
    name = place.get("name", "")
    if types & DROP_TYPES:
        return False
    if DROP_NAME_PATTERNS.search(name):
        return False
    if types & KEEP_TYPES:
        return True
    return False


def collect_candidates(resume: bool) -> dict[str, dict]:
    progress = {}
    if resume and PROGRESS_FILE.exists():
        progress = json.loads(PROGRESS_FILE.read_text())

    candidates: dict[str, dict] = {}
    grid = build_grid()
    total = len(grid) * len(SEARCH_TYPES)
    done = 0

    for lat, lon, cell_id in grid:
        for stype in SEARCH_TYPES:
            key = f"{cell_id}_{stype}"
            done += 1
            if key in progress:
                for pid, place in progress[key].items():
                    candidates[pid] = place
                continue

            cell_results: dict[str, dict] = {}
            page_token = None
            while True:
                try:
                    resp = nearby_search(lat, lon, stype, page_token)
                except Exception as e:
                    print(f"  Error {key}: {e}")
                    break

                for place in resp.get("results", []):
                    if is_food_place(place):
                        cell_results[place["place_id"]] = place

                page_token = resp.get("next_page_token")
                if not page_token:
                    break
                time.sleep(2)

            progress[key] = cell_results
            for pid, place in cell_results.items():
                candidates[pid] = place
            PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False))
            time.sleep(PAUSE_S)

        if done % 30 == 0:
            print(f"  Grid: {done}/{total}  candidates so far: {len(candidates)}")

    return candidates


def enrich_with_details(candidates: dict[str, dict],
                        existing_ids: set[str]) -> list[dict]:
    results = []
    items = [(pid, p) for pid, p in candidates.items() if pid not in existing_ids]
    print(f"Fetching details for {len(items)} new places…")

    for i, (place_id, _) in enumerate(items):
        try:
            det = place_details(place_id).get("result", {})
        except Exception as e:
            print(f"  Details error {place_id}: {e}")
            time.sleep(1)
            continue

        geom = det.get("geometry", {}).get("location", {})
        lat = geom.get("lat")
        lon = geom.get("lng")
        if not lat or not lon:
            continue

        photos = det.get("photos", [])
        photo_url = ""
        if photos and photos[0].get("photo_reference"):
            photo_url = make_photo_url(photos[0]["photo_reference"])

        restaurant = {
            "place_id": place_id,
            "name": det.get("name", ""),
            "address": det.get("formatted_address", ""),
            "lat": lat,
            "lon": lon,
            "opening_hours": det.get("opening_hours"),
            "rating": det.get("rating"),
            "reviews_count": det.get("user_ratings_total"),
            "price_level": det.get("price_level"),
            "website": det.get("website", ""),
            "phone": det.get("formatted_phone_number", ""),
            "photo_url": photo_url,
            "district": "wrangelkiez",
            "kbju_status": "no_data",
            "wolt_status": "unknown",
            "wolt_menu": [],
            "site_menu": [],
        }
        results.append(restaurant)

        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(items)} details fetched")
        time.sleep(PAUSE_S)

    return results


def run(resume: bool) -> None:
    existing: list[dict] = []
    if OUT_FILE.exists():
        existing = json.loads(OUT_FILE.read_text())
    existing_ids = {r["place_id"] for r in existing}
    print(f"Уже в базе: {len(existing)} ресторанов")

    print("Шаг 1: сбор кандидатов через Nearby Search…")
    candidates = collect_candidates(resume)
    print(f"Кандидатов найдено: {len(candidates)}")
    new_count = len([p for p in candidates if p not in existing_ids])
    print(f"Новых (нет в базе): {new_count}")

    print("\nШаг 2: обогащение деталями…")
    new_restaurants = enrich_with_details(candidates, existing_ids)

    all_restaurants = existing + new_restaurants
    OUT_FILE.write_text(json.dumps(all_restaurants, ensure_ascii=False, indent=2))

    print(f"\nГотово. Всего ресторанов: {len(all_restaurants)}")
    print(f"Файл: {OUT_FILE}")

    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()


if __name__ == "__main__":
    resume = "--resume" in sys.argv
    run(resume)
