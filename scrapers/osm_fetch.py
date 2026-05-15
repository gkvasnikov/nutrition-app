#!/usr/bin/env python3
"""Fetch restaurants and cafes from Overpass API for Berlin Mitte bbox."""

from __future__ import annotations

import json
import time
import logging
from typing import Optional, Tuple
import requests
from pathlib import Path

ROOT = Path(__file__).parent.parent

logging.basicConfig(
    filename=ROOT / "errors.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

BBOX = (52.516, 13.388, 52.526, 13.408)  # south, west, north, east

OUTPUT_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup.json"

QUERY = """
[out:json][timeout:60];
(
  node["amenity"~"^(restaurant|cafe|fast_food|food_court|bar|pub)$"]
    ({south},{west},{north},{east});
  way["amenity"~"^(restaurant|cafe|fast_food|food_court|bar|pub)$"]
    ({south},{west},{north},{east});
  relation["amenity"~"^(restaurant|cafe|fast_food|food_court|bar|pub)$"]
    ({south},{west},{north},{east});
);
out center tags;
""".format(south=BBOX[0], west=BBOX[1], north=BBOX[2], east=BBOX[3])


def build_address(tags: dict) -> str:
    parts = []
    street = tags.get("addr:street", "")
    housenumber = tags.get("addr:housenumber", "")
    if street:
        parts.append(f"{street} {housenumber}".strip())
    city = tags.get("addr:city", "")
    postcode = tags.get("addr:postcode", "")
    if postcode or city:
        parts.append(f"{postcode} {city}".strip())
    return ", ".join(p for p in parts if p)


def extract_coords(element: dict) -> Tuple[Optional[float], Optional[float]]:
    etype = element.get("type")
    if etype == "node":
        return element.get("lat"), element.get("lon")
    # way and relation have a "center" key
    center = element.get("center", {})
    return center.get("lat"), center.get("lon")


def fetch_overpass() -> list[dict]:
    print("Fetching from Overpass API…")
    try:
        resp = requests.post(
            OVERPASS_URL,
            data={"data": QUERY},
            timeout=90,
            headers={"User-Agent": "NutritionApp/0.1 (george@gkvasnikov.com)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logging.error("Overpass request failed: %s", exc)
        raise

    return resp.json().get("elements", [])


def parse_elements(elements: list[dict]) -> list[dict]:
    restaurants = []
    seen_ids = set()

    for el in elements:
        tags = el.get("tags", {})
        osm_id = f"{el['type']}/{el['id']}"

        if osm_id in seen_ids:
            continue
        seen_ids.add(osm_id)

        lat, lon = extract_coords(el)
        if lat is None or lon is None:
            logging.error("No coords for element %s, skipping", osm_id)
            continue

        name = tags.get("name") or tags.get("name:en") or tags.get("brand") or ""
        if not name:
            continue  # skip unnamed venues

        restaurants.append(
            {
                "id": osm_id,
                "name": name,
                "lat": lat,
                "lon": lon,
                "cuisine": tags.get("cuisine", ""),
                "website": tags.get("website") or tags.get("contact:website") or "",
                "address": build_address(tags),
                "source": "osm",
                "menu": [],
            }
        )

    return restaurants


def save(restaurants: list[dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if OUTPUT_FILE.exists():
        BACKUP_FILE.write_bytes(OUTPUT_FILE.read_bytes())
        print(f"Backup saved to {BACKUP_FILE}")

    OUTPUT_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"Saved {len(restaurants)} restaurants to {OUTPUT_FILE}")


def main() -> None:
    time.sleep(1)  # polite delay before first request
    elements = fetch_overpass()
    print(f"Got {len(elements)} OSM elements")

    restaurants = parse_elements(elements)
    print(f"Parsed {len(restaurants)} named venues")

    save(restaurants)


if __name__ == "__main__":
    main()
