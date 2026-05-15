#!/usr/bin/env python3
"""
Enrich restaurants.json with Google Places data.

For each restaurant:
  - Find Place by Text (name + address)
  - Update: google_place_id, rating, reviews_count, price_level,
    opening_hours, phone, website, photo_url
  - Remove if not found on Google Maps

Resume: skips restaurants that already have google_place_id.

Usage:
  python3 enrichment/google_places_enricher.py
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
import os

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup_gplaces.json"
LOG_FILE = ROOT / "enrichment.log"
ENV_FILE = ROOT / ".env"

load_dotenv(ENV_FILE)
API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PHOTO_URL = "https://maps.googleapis.com/maps/api/place/photo"
PAUSE = 0.3


BERLIN_BIAS = "circle:8000@52.521,13.398"


def find_place(name: str, address: str) -> Optional[dict]:
    query = f"{name} {address} Berlin".strip()
    params = {
        "input": query,
        "inputtype": "textquery",
        "fields": "place_id,name,rating,user_ratings_total,price_level,opening_hours,photos",
        "locationbias": BERLIN_BIAS,
        "key": API_KEY,
    }
    try:
        r = requests.get(FIND_PLACE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            log.warning("Places API status %s for %r: %s", status, name, data.get("error_message",""))
        candidates = data.get("candidates", [])
        if not candidates:
            return None
        return candidates[0]
    except Exception as exc:
        log.error("Find place error for %r: %s", name, exc)
        return None


def get_place_details(place_id: str) -> Optional[dict]:
    params = {
        "place_id": place_id,
        "fields": "opening_hours,formatted_phone_number,website,photos",
        "key": API_KEY,
    }
    try:
        r = requests.get(PLACE_DETAILS_URL, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result", {})
    except Exception as exc:
        log.error("Place details error for %r: %s", place_id, exc)
        return None


def make_photo_url(photo_reference: str, max_width: int = 800) -> str:
    return (
        f"{PHOTO_URL}?maxwidth={max_width}"
        f"&photo_reference={photo_reference}"
        f"&key={API_KEY}"
    )


def enrich_restaurant(restaurant: dict, place: dict) -> None:
    restaurant["google_place_id"] = place.get("place_id")
    if place.get("rating") is not None:
        restaurant["rating"] = place["rating"]
    if place.get("user_ratings_total") is not None:
        restaurant["reviews_count"] = place["user_ratings_total"]
    if place.get("price_level") is not None:
        restaurant["price_level"] = place["price_level"]
    if place.get("formatted_phone_number"):
        restaurant["phone"] = place["formatted_phone_number"]
    if place.get("website") and not restaurant.get("website"):
        restaurant["website"] = place["website"]

    # Opening hours
    oh = place.get("opening_hours", {})
    if oh.get("weekday_text"):
        restaurant["opening_hours"] = oh["weekday_text"]

    # Photo
    photos = place.get("photos", [])
    if photos and photos[0].get("photo_reference"):
        restaurant["photo_url"] = make_photo_url(photos[0]["photo_reference"])


def main() -> None:
    if not API_KEY:
        print("ERROR: GOOGLE_PLACES_API_KEY not set in .env")
        return

    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    total = len(restaurants)

    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE}")
    print(f"Total restaurants: {total}")

    already_done = sum(1 for r in restaurants if r.get("google_place_id"))
    print(f"Already processed (have google_place_id): {already_done}")
    print(f"To process: {total - already_done}\n")

    updated = 0
    removed_list = []
    n = 0

    i = 0
    while i < len(restaurants):
        r = restaurants[i]
        if r.get("google_place_id"):
            i += 1
            continue

        n += 1
        name = r.get("name", "")
        address = r.get("address", "")
        print(f"[{n}/{total - already_done}] {name} — {address[:50]}", end=" ", flush=True)

        place = find_place(name, address)
        time.sleep(PAUSE)

        if place:
            # Get full details (opening hours may need details call)
            if not place.get("opening_hours") or not place.get("formatted_phone_number"):
                details = get_place_details(place["place_id"])
                time.sleep(PAUSE)
                if details:
                    for key in ("opening_hours", "formatted_phone_number", "website", "photos"):
                        if details.get(key) and not place.get(key):
                            place[key] = details[key]

            enrich_restaurant(r, place)
            print(f"✓ {place.get('name','')} | {r.get('rating','?')}★ ({r.get('reviews_count','?')} reviews)")
            log.info("FOUND: %s → place_id=%s rating=%s", name, r.get("google_place_id"), r.get("rating"))
            updated += 1
            i += 1
        else:
            print(f"✗ NOT FOUND — removing")
            log.info("NOT FOUND (removed): %r | %r", name, address)
            removed_list.append({"name": name, "address": address})
            restaurants.pop(i)
            # don't increment i — next restaurant slides into this index

        # Save after every restaurant
        DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))

    print(f"\n{'='*50}")
    print(f"Updated:  {updated}")
    print(f"Removed:  {len(removed_list)}")
    print(f"Remaining in DB: {len(restaurants)}")
    if removed_list:
        print("\nRemoved restaurants:")
        for r in removed_list:
            print(f"  - {r['name']} | {r['address']}")

    log.info(
        "google_places_enricher complete. updated=%d removed=%d remaining=%d",
        updated, len(removed_list), len(restaurants),
    )


if __name__ == "__main__":
    main()
