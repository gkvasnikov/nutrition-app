#!/usr/bin/env python3
"""
Сканирует рестораны из data/kreuzberg_north_patch/restaurants.json на Wolt
и скрапит меню через Playwright.

Алгоритм:
  1. Один раз собирает все Wolt-заведения в bbox north patch
     (сетка запросов, дедупликация по slug, кэш в .wolt_cache.json)
  2. Fuzzy-матчинг по названию (порог 75) + расстояние ≤ 600м
  3. Меню парсится через Playwright: name, description, price
  4. wolt_menu добавляется к ресторану, сохранение каждые 20

Resume: пропускает рестораны у которых уже есть wolt_menu.

Использование:
  python3 scrapers/kreuzberg_north_patch/wolt_scanner.py
  python3 scrapers/kreuzberg_north_patch/wolt_scanner.py --resume
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from fuzzywuzzy import fuzz
from playwright.sync_api import sync_playwright, Page


def _find_env() -> Path:
    c = Path(__file__).parent
    for _ in range(8):
        if (c / ".env").exists():
            return c / ".env"
        c = c.parent
    return Path(__file__).parent / ".env"


load_dotenv(_find_env())

# ── Пути ───────────────────────────────────────────────────────────────────

ROOT             = Path(__file__).parent.parent.parent
RESTAURANTS_FILE = ROOT / "data" / "kreuzberg_north_patch" / "restaurants.json"
CACHE_FILE       = ROOT / "data" / "kreuzberg_north_patch" / ".wolt_cache.json"
LOG_FILE         = ROOT / "logs" / "wolt_patch.log"

# ── Логгер ─────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────────

# Расширяем bbox чуть больше ареала north patch, чтобы захватить Wolt-заведения
# на краях (ресторан может быть немного за bbox, но матчиться по расстоянию)
BBOX = (52.488, 13.395, 52.516, 13.465)

MATCH_THRESHOLD = 75
MAX_DISTANCE_M  = 600
SAVE_EVERY      = 20

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

NOISE_TOKENS = {
    "restaurant", "restaurants", "cafe", "café", "coffee", "imbiss",
    "berlin", "kreuzberg", "friedrichshain", "mitte", "kitchen", "food",
    "gmbh", "und", "the", "das", "die", "der", "am", "im",
}


# ── I/O ────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


# ── Wolt venue collection ──────────────────────────────────────────────────

def _fetch_venues_near(lat: float, lon: float) -> list[dict]:
    try:
        resp = requests.get(
            WOLT_SEARCH_URL,
            params={"lat": lat, "lon": lon, "limit": 50},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        venues = []
        for section in data.get("sections", []):
            for item in section.get("items", []):
                v = item.get("venue") or (item.get("track_id") and item)
                if isinstance(v, dict) and v.get("slug"):
                    venues.append(v)
        return venues
    except Exception as e:
        log.error("Wolt API error at (%.4f, %.4f): %s", lat, lon, e)
        return []


def collect_wolt_venues() -> dict[str, dict]:
    """Обходит bbox сеткой, собирает все slug → venue. Кэширует."""
    if CACHE_FILE.exists():
        cached = json.loads(CACHE_FILE.read_text())
        print(f"Wolt кэш: {len(cached)} заведений (из {CACHE_FILE.name})")
        return cached

    print("Собираем Wolt-заведения в bbox north patch…")
    center_lat = (BBOX[0] + BBOX[2]) / 2
    lat_step   = 500 / 111_000
    lon_step   = 500 / (111_000 * math.cos(math.radians(center_lat)))

    all_venues: dict[str, dict] = {}
    lat = BBOX[0] + lat_step / 2
    while lat < BBOX[2]:
        lon = BBOX[1] + lon_step / 2
        while lon < BBOX[3]:
            venues = _fetch_venues_near(lat, lon)
            for v in venues:
                slug = v.get("slug", "")
                if slug and slug not in all_venues:
                    all_venues[slug] = v
            time.sleep(random.uniform(1.0, 2.0))
            lon += lon_step
        lat += lat_step

    print(f"  найдено {len(all_venues)} уникальных Wolt-заведений")
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(all_venues, ensure_ascii=False, indent=2))
    return all_venues


# ── Matching ────────────────────────────────────────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def _meaningful_tokens(name: str) -> set[str]:
    return {t for t in _normalize(name).split() if t not in NOISE_TOKENS and len(t) > 2}


def _venue_coords(venue: dict) -> Optional[tuple[float, float]]:
    loc = venue.get("location")
    if isinstance(loc, list) and len(loc) == 2:
        return loc[1], loc[0]   # Wolt: [lon, lat]
    if isinstance(loc, dict):
        return loc.get("lat"), loc.get("lon")
    return None


def best_wolt_match(restaurant: dict,
                    wolt_venues: dict[str, dict],
                    used_slugs: set[str]) -> Optional[dict]:
    r_name   = _normalize(restaurant["name"])
    r_tokens = _meaningful_tokens(restaurant["name"])
    r_lat, r_lon = restaurant.get("lat"), restaurant.get("lon")
    if r_lat is None or r_lon is None:
        return None

    best_venue, best_score = None, 0

    for slug, venue in wolt_venues.items():
        if slug in used_slugs:
            continue

        w_name  = _normalize(venue.get("name", ""))
        score   = fuzz.token_set_ratio(r_name, w_name)
        if score <= MATCH_THRESHOLD:
            continue

        # хотя бы один значимый токен должен совпасть
        w_tokens = _meaningful_tokens(venue.get("name", ""))
        if r_tokens and w_tokens and not (r_tokens & w_tokens):
            continue

        # проверка расстояния
        coords = _venue_coords(venue)
        if coords and coords[0] is not None:
            dist = haversine_m(r_lat, r_lon, coords[0], coords[1])
            if dist > MAX_DISTANCE_M:
                continue

        if score > best_score:
            best_score = score
            best_venue = venue

    return best_venue


# ── Menu scraping ───────────────────────────────────────────────────────────

def _parse_price(text: str) -> Optional[float]:
    m = re.search(r"[\d]+[.,]?\d*", text.replace(",", "."))
    return float(m.group().replace(",", ".")) if m else None


def scrape_menu(page: Page, slug: str) -> list[dict]:
    url = WOLT_VENUE_URL.format(slug=slug)
    try:
        page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception:
        page.wait_for_timeout(3000)

    # Убираем cookie-баннер
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
            name     = name_el.inner_text().strip() if name_el else ""
            desc     = desc_el.inner_text().strip() if desc_el else ""
            price_raw = (price_el.get_attribute("aria-label") or
                         price_el.inner_text()) if price_el else ""
            price    = _parse_price(price_raw)
            if name:
                items.append({
                    "name":        name,
                    "description": desc,
                    "price":       price,
                    "calories":    None,
                    "protein":     None,
                    "fat":         None,
                    "carbs":       None,
                })
        except Exception:
            continue
    return items


# ── Main ───────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    restaurants: list[dict] = load_json(RESTAURANTS_FILE)
    if not restaurants:
        print(f"Файл не найден или пустой: {RESTAURANTS_FILE}")
        return

    wolt_venues = collect_wolt_venues()
    if not wolt_venues:
        print("Wolt не вернул заведений. Проверь подключение.")
        return

    used_slugs: set[str] = {r["wolt_slug"] for r in restaurants if r.get("wolt_slug")}

    total      = len(restaurants)
    found      = 0
    not_found  = 0
    total_dishes = 0
    saved_count  = 0

    print(f"\nРесторанов к обработке : {total}")
    print(f"Уже с wolt_menu        : {sum(1 for r in restaurants if r.get('wolt_menu'))}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})

        for i, r in enumerate(restaurants, 1):
            # Resume: пропускаем если уже обработан (найден или отмечен как not_found)
            if r.get("wolt_slug") or r.get("wolt_scanned"):
                continue

            name = r.get("name", "?")
            print(f"[{i}/{total}] {name[:50]}", end=" … ", flush=True)

            venue = best_wolt_match(r, wolt_venues, used_slugs)

            if venue is None:
                r["wolt_menu"]    = []
                r["wolt_scanned"] = True
                not_found += 1
                print("не найден")
                log.info("NOT_FOUND | %s | %s", name, r.get("address", ""))
                saved_count += 1
                if saved_count % SAVE_EVERY == 0:
                    save_json(RESTAURANTS_FILE, restaurants)
                    print(f"  ── checkpoint ({saved_count} обработано) ──")
                continue

            slug = venue["slug"]
            used_slugs.add(slug)
            r["wolt_slug"] = slug
            print(f"→ wolt:{slug}", end=" ", flush=True)

            items = scrape_menu(page, slug)
            r["wolt_menu"]    = items
            r["wolt_scanned"] = True

            if items:
                found       += 1
                total_dishes += len(items)
                print(f"{len(items)} блюд")
                log.info("FOUND | %s | slug=%s | dishes=%d", name, slug, len(items))
            else:
                not_found += 1
                print("меню пустое")
                log.warning("EMPTY_MENU | %s | slug=%s", name, slug)

            # Сохраняем каждые SAVE_EVERY ресторанов
            saved_count += 1
            if saved_count % SAVE_EVERY == 0:
                save_json(RESTAURANTS_FILE, restaurants)
                print(f"  ── checkpoint ({saved_count} обработано) ──")

            time.sleep(random.uniform(1.0, 2.0))

        browser.close()

    # Финальное сохранение
    save_json(RESTAURANTS_FILE, restaurants)

    print(f"\n{'─' * 50}")
    print(f"Ресторанов найдено на Wolt : {found} / {found + not_found}")
    print(f"Всего блюд собрано         : {total_dishes}")
    print(f"Файл                       : {RESTAURANTS_FILE}")
    print(f"Лог                        : {LOG_FILE}")
    log.info("DONE | found=%d not_found=%d dishes=%d", found, not_found, total_dishes)


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
