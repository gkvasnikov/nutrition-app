#!/usr/bin/env python3
"""
Scrape Wolt menus for Berlin Mitte restaurants via Playwright.

Steps:
  1. Fetch venue list from Wolt v1/pages/restaurants API (bbox filter)
  2. For each venue open wolt.com in headless Chromium, extract menu cards
  3. Fuzzy-match each Wolt venue to restaurants.json by name + address
  4. Write wolt_menu: [{name, description, price, weight}] into matched entries
  5. Save updated restaurants.json (with backup)
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests
from fuzzywuzzy import fuzz
from playwright.sync_api import sync_playwright, Page

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup.json"

logging.basicConfig(
    filename=ROOT / "errors.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

BBOX = {"south": 52.516, "west": 13.388, "north": 52.526, "east": 13.408}
WOLT_LIST_URL = "https://restaurant-api.wolt.com/v1/pages/restaurants?lat=52.521&lon=13.398"
WOLT_VENUE_URL = "https://wolt.com/en/deu/berlin/restaurant/{slug}"

MATCH_THRESHOLD = 65  # min fuzzy score to consider a match


# ---------------------------------------------------------------------------
# Step 1 — fetch Wolt venue list
# ---------------------------------------------------------------------------

def fetch_wolt_venues() -> list[dict]:
    """Return Wolt venues inside the Mitte bbox."""
    print("Fetching Wolt venue list…")
    r = requests.get(
        WOLT_LIST_URL,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    items = r.json()["sections"][1]["items"]

    in_bbox = [
        i["venue"] for i in items
        if BBOX["south"] <= i["venue"]["location"][1] <= BBOX["north"]
        and BBOX["west"] <= i["venue"]["location"][0] <= BBOX["east"]
    ]
    print(f"  {len(in_bbox)} Wolt venues inside bbox")
    return in_bbox


# ---------------------------------------------------------------------------
# Step 2 — scrape menu from wolt.com page
# ---------------------------------------------------------------------------

def _dismiss_banner(page: Page) -> None:
    page.eval_on_selector_all(
        "[data-test-id='consents-banner-overlay']",
        "els => els.forEach(e => e.remove())",
    )


def _parse_price(price_text: str) -> Optional[float]:
    m = re.search(r"[\d,\.]+", price_text.replace(",", "."))
    return float(m.group()) if m else None


def _extract_weight(text: str) -> str:
    """Try to extract weight/volume from name or description."""
    m = re.search(r"\b(\d+)\s*(g|ml|cl|l)\b", text, re.I)
    return f"{m.group(1)}{m.group(2).lower()}" if m else ""


def scrape_menu(page: Page, slug: str) -> list[dict]:
    url = WOLT_VENUE_URL.format(slug=slug)
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as exc:
        logging.error("Timeout loading %s: %s", url, exc)
        return []

    _dismiss_banner(page)
    page.wait_for_timeout(500)

    cards = page.query_selector_all("[data-test-id='horizontal-item-card']")
    items = []
    for card in cards:
        try:
            name_el = card.query_selector("[data-test-id='horizontal-item-card-header']")
            desc_el = card.query_selector("p")
            price_el = card.query_selector("[data-test-id='horizontal-item-card-price']")
            img_el = card.query_selector("img")

            name = name_el.inner_text().strip() if name_el else ""
            desc = desc_el.inner_text().strip() if desc_el else ""
            price_raw = price_el.get_attribute("aria-label") or price_el.inner_text() if price_el else ""
            price = _parse_price(price_raw)
            weight = _extract_weight(name + " " + desc)
            image_url = ""
            if img_el:
                image_url = img_el.get_attribute("src") or img_el.get_attribute("data-src") or ""

            if name:
                items.append({
                    "name": name,
                    "description": desc,
                    "price": price,
                    "weight": weight,
                    "image_url": image_url,
                })
        except Exception as exc:
            logging.error("Card parse error for %s: %s", slug, exc)

    return items


# ---------------------------------------------------------------------------
# Step 3 — fuzzy match Wolt venue → restaurants.json entry
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def _score(wolt_venue: dict, osm: dict) -> int:
    name_score = fuzz.token_set_ratio(
        _normalize(wolt_venue.get("name", "")),
        _normalize(osm.get("name", "")),
    )
    addr_score = 0
    if wolt_venue.get("address") and osm.get("address"):
        # Compare street name only (first word)
        wolt_street = _normalize(wolt_venue["address"].split()[0])
        osm_street = _normalize(osm["address"].split()[0])
        addr_score = fuzz.ratio(wolt_street, osm_street)
    return name_score + addr_score // 4  # name dominates


def find_best_match(wolt_venue: dict, restaurants: list[dict]) -> Optional[int]:
    """Return index in restaurants list or None if no good match."""
    best_idx, best_score = None, 0
    for i, r in enumerate(restaurants):
        s = _score(wolt_venue, r)
        if s > best_score:
            best_score = s
            best_idx = i
    if best_score >= MATCH_THRESHOLD:
        return best_idx
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Load restaurants.json
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    print(f"Loaded {len(restaurants)} restaurants from {DATA_FILE}")

    # Backup
    BACKUP_FILE.write_bytes(DATA_FILE.read_bytes())
    print(f"Backup saved to {BACKUP_FILE}")

    # Fetch Wolt venue list
    time.sleep(1)
    wolt_venues = fetch_wolt_venues()

    matched = 0
    unmatched_venues: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        for idx, venue in enumerate(wolt_venues, 1):
            slug = venue.get("slug", "")
            name = venue.get("name", slug)
            print(f"[{idx}/{len(wolt_venues)}] {name} ({slug})")

            # Scrape menu
            menu = scrape_menu(page, slug)
            if not menu:
                print(f"  — no menu items, skipping")
                unmatched_venues.append(f"{name} (no menu)")
                time.sleep(1)
                continue

            print(f"  scraped {len(menu)} items")

            # Match to restaurants.json
            match_idx = find_best_match(venue, restaurants)
            if match_idx is None:
                print(f"  — no OSM match (threshold {MATCH_THRESHOLD})")
                unmatched_venues.append(name)
            else:
                osm_name = restaurants[match_idx]["name"]
                restaurants[match_idx]["wolt_menu"] = menu
                print(f"  matched → '{osm_name}'")
                matched += 1

            time.sleep(1.5)

        browser.close()

    # Save
    DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"\nDone. Matched {matched}/{len(wolt_venues)} venues.")
    print(f"Saved to {DATA_FILE}")

    if unmatched_venues:
        print(f"\nUnmatched ({len(unmatched_venues)}):")
        for v in unmatched_venues:
            print(f"  {v}")


if __name__ == "__main__":
    main()
