#!/usr/bin/env python3
"""
Fetch photos for restaurants that have a menu but no photo_url.
Uses Google Places Details API (fields=photos only → cheapest call).

Cost: ~$17/1000 → ~$4.5 for 266 restaurants.
Resume: skips restaurants that already have photo_url.
"""

from __future__ import annotations
import json, os, time, shutil
from pathlib import Path
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "all_restaurants.json"
BACKUP_FILE = ROOT / "data" / "all_restaurants_backup_photos.json"

def find_env() -> Path:
    c = ROOT
    for _ in range(6):
        if (c / ".env").exists(): return c / ".env"
        c = c.parent
    return ROOT / ".env"

load_dotenv(find_env())
API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
PHOTO_URL   = "https://maps.googleapis.com/maps/api/place/photo"
MAX_PHOTOS  = 3
PAUSE       = 0.3


def build_photo_url(ref: str) -> str:
    return f"{PHOTO_URL}?maxwidth=800&photo_reference={ref}&key={API_KEY}"


def fetch_photos(place_id: str) -> list[str]:
    r = requests.get(DETAILS_URL, params={
        "place_id": place_id,
        "fields": "photos",
        "key": API_KEY,
    }, timeout=10)
    r.raise_for_status()
    data = r.json()
    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        return []
    photos_raw = data.get("result", {}).get("photos", [])
    return [build_photo_url(p["photo_reference"]) for p in photos_raw[:MAX_PHOTOS] if "photo_reference" in p]


def save(restaurants: list, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    tmp.replace(path)


def main() -> None:
    if not API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set")

    restaurants = json.loads(DATA_FILE.read_text())
    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE.name}")

    targets = [
        (i, r) for i, r in enumerate(restaurants)
        if (r.get("wolt_menu") or r.get("site_menu"))
        and not r.get("photo_url")
        and not r.get("photos")
        and (r.get("google_place_id") or r.get("place_id"))
    ]
    print(f"Restaurants needing photos: {len(targets)}\n")

    done = errors = 0
    for n, (i, r) in enumerate(targets, 1):
        pid = r.get("google_place_id") or r.get("place_id")
        urls = fetch_photos(pid)
        if urls:
            restaurants[i]["photo_url"] = urls[0]
            if len(urls) > 1:
                restaurants[i]["photos"] = urls
            done += 1
            print(f"[{n}/{len(targets)}] {r['name'][:40]} → {len(urls)} photo(s)")
        else:
            errors += 1
            print(f"[{n}/{len(targets)}] {r['name'][:40]} — no photos")

        time.sleep(PAUSE)

        if n % 50 == 0:
            save(restaurants, DATA_FILE)
            print(f"  Checkpoint saved ({n}/{len(targets)})\n")

    save(restaurants, DATA_FILE)
    print(f"\nDone. Got photos: {done}  No photos: {errors}")


if __name__ == "__main__":
    main()
