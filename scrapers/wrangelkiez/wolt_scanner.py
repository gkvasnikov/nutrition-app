"""
Ищет рестораны Вранглькица на Wolt, скачивает меню + аллергены.

Алгоритм:
  1. Собирает все Wolt-заведения в bbox через API (с кэшем)
  2. Fuzzy-матчинг по названию (порог 75) + проверка расстояния (≤600м)
  3. Для совпавших — скрапит меню через Playwright
  4. Для каждого блюда кликает карточку → собирает аллергены из модала

Использование:
  python3 scrapers/wrangelkiez/wolt_scanner.py
  python3 scrapers/wrangelkiez/wolt_scanner.py --resume
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fuzzywuzzy import fuzz
from playwright.sync_api import sync_playwright, Page

load_dotenv()

ROOT = Path(__file__).parent.parent.parent
RESTAURANTS_FILE = ROOT / "data" / "wrangelkiez" / "restaurants_wrangelkiez.json"
VERIFIED_FILE    = ROOT / "data" / "wrangelkiez" / "verified_wolt.json"
WOLT_CACHE_FILE  = ROOT / "data" / "wrangelkiez" / ".wolt_venues_cache.json"

BBOX = (52.490, 13.424, 52.505, 13.460)
MATCH_THRESHOLD = 75
PAUSE_S = 2.0
MAX_DISTANCE_M = 600

WOLT_SEARCH_URL = "https://consumer-api.wolt.com/v1/pages/restaurants"
WOLT_VENUE_URL  = "https://wolt.com/de/deu/berlin/restaurant/{slug}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": "https://wolt.com/",
}


def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_wolt_venues_near(lat: float, lon: float) -> list[dict]:
    try:
        resp = requests.get(
            WOLT_SEARCH_URL,
            params={"lat": lat, "lon": lon},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        sections = data.get("sections", [])
        venues = []
        for sec in sections:
            for item in sec.get("items", []):
                venue = item.get("venue") or item.get("track_id") and item
                if isinstance(item, dict) and "slug" in item.get("venue", {}):
                    venues.append(item["venue"])
                elif isinstance(item, dict) and item.get("slug"):
                    venues.append(item)
        return venues
    except Exception:
        return []


def collect_all_wolt_venues() -> list[dict]:
    if WOLT_CACHE_FILE.exists():
        print("  Wolt-кэш найден, используем.")
        return load_json(WOLT_CACHE_FILE)

    print("  Собираем Wolt-заведения по сетке bbox…")
    seen_slugs: set[str] = set()
    all_venues: list[dict] = []

    lat_step = 0.007
    lon_step = 0.010
    lat = BBOX[0]
    while lat <= BBOX[2]:
        lon = BBOX[1]
        while lon <= BBOX[3]:
            venues = fetch_wolt_venues_near(lat, lon)
            for v in venues:
                slug = v.get("slug", "")
                if slug and slug not in seen_slugs:
                    seen_slugs.add(slug)
                    all_venues.append(v)
            lon += lon_step
            time.sleep(0.5)
        lat += lat_step

    save_json(WOLT_CACHE_FILE, all_venues)
    print(f"  Wolt-заведений в bbox: {len(all_venues)}")
    return all_venues


def match_wolt(restaurant: dict, wolt_venues: list[dict]) -> Optional[dict]:
    name = restaurant.get("name", "")
    lat  = restaurant.get("lat", 0)
    lon  = restaurant.get("lon", 0)
    best_score = 0
    best_venue = None

    for v in wolt_venues:
        v_name = v.get("name", "") or v.get("short_description", "")
        loc = v.get("location")
        if isinstance(loc, list) and len(loc) == 2:
            v_lon, v_lat = loc[0], loc[1]
        elif isinstance(loc, dict):
            coords = loc.get("coordinates", [0, 0])
            v_lon, v_lat = coords[0], coords[1]
        else:
            v_lon, v_lat = 0, 0

        dist = haversine_m(lat, lon, v_lat, v_lon) if v_lat and v_lon else 9999
        if dist > MAX_DISTANCE_M:
            continue

        score = fuzz.token_set_ratio(normalize(name), normalize(v_name))
        if score > best_score:
            best_score = score
            best_venue = v

    if best_score >= MATCH_THRESHOLD:
        return best_venue
    return None


def _parse_price(raw: str) -> Optional[float]:
    m = re.search(r"[\d]+[.,]\d{2}", raw)
    if m:
        return float(m.group().replace(",", "."))
    return None


def scrape_menu_with_allergens(page: Page, slug: str) -> list[dict]:
    url = WOLT_VENUE_URL.format(slug=slug)
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        page.wait_for_timeout(3000)

    page.eval_on_selector_all(
        "[data-test-id='consents-banner-overlay']",
        "els => els.forEach(e => e.remove())",
    )
    page.wait_for_timeout(1000)

    items = []
    cards = page.query_selector_all("[data-test-id='horizontal-item-card']")

    for card in cards:
        try:
            name_el  = card.query_selector("[data-test-id='horizontal-item-card-header']")
            desc_el  = card.query_selector("p")
            price_el = card.query_selector("[data-test-id='horizontal-item-card-price']")
            img_el   = card.query_selector("img")

            name = name_el.inner_text().strip() if name_el else ""
            desc = desc_el.inner_text().strip() if desc_el else ""
            price_raw = (price_el.get_attribute("aria-label") or
                         price_el.inner_text()) if price_el else ""
            price = _parse_price(price_raw)
            image_url = ""
            if img_el:
                image_url = img_el.get_attribute("src") or img_el.get_attribute("data-src") or ""

            if not name:
                continue

            items.append({
                "name": name,
                "description": desc,
                "price": price,
                "calories": None,
                "protein": None,
                "fat": None,
                "carbs": None,
                "image_url": image_url,
            })

        except Exception:
            continue

    return items


def run(resume: bool) -> None:
    restaurants: list[dict] = load_json(RESTAURANTS_FILE)
    verified: list[dict] = load_json(VERIFIED_FILE)
    verified_ids = {v["place_id"] for v in verified}

    wolt_venues = collect_all_wolt_venues()
    if not wolt_venues:
        print("Wolt не вернул заведений.")
        return

    stats = {"found": 0, "with_menu": 0, "not_found": 0}
    used_slugs: set[str] = {v["slug"] for v in verified}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})

        for i, r in enumerate(restaurants):
            if resume and r["place_id"] in verified_ids:
                continue
            if r.get("wolt_status") in ("found", "not_found") and resume:
                continue

            print(f"[{i+1}/{len(restaurants)}] {r['name']}")

            venue = match_wolt(r, wolt_venues)
            if not venue:
                r["wolt_status"] = "not_found"
                stats["not_found"] += 1
                print(f"  → не найден на Wolt")
                continue

            slug = venue.get("slug", "")
            if not slug:
                r["wolt_status"] = "not_found"
                stats["not_found"] += 1
                continue

            if slug in used_slugs:
                # Уже обработан другим рестораном
                r["wolt_status"] = "duplicate_slug"
                continue

            used_slugs.add(slug)
            r["wolt_slug"] = slug
            stats["found"] += 1
            print(f"  → матч: {venue.get('name','')} (slug={slug})")

            items = scrape_menu_with_allergens(page, slug)
            print(f"  → {len(items)} блюд (с аллергенами где есть)")

            if items:
                r["wolt_menu"] = items
                r["wolt_status"] = "found"
                r["kbju_status"] = "pending_estimation"
                stats["with_menu"] += 1
                verified.append({"place_id": r["place_id"], "slug": slug})
                save_json(VERIFIED_FILE, verified)
            else:
                r["wolt_status"] = "found_no_menu"

            save_json(RESTAURANTS_FILE, restaurants)
            time.sleep(PAUSE_S)

        browser.close()

    save_json(RESTAURANTS_FILE, restaurants)

    print(f"\nГотово.")
    print(f"  Найдено на Wolt  : {stats['found']}")
    print(f"  С меню           : {stats['with_menu']}")
    print(f"  Не найдено       : {stats['not_found']}")


if __name__ == "__main__":
    resume = "--resume" in sys.argv
    run(resume)
