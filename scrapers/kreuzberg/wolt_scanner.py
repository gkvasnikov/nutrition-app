"""
Ищет рестораны Кройцберга на Wolt, скачивает меню и КБЖУ.

Алгоритм:
  1. Один раз собирает все Wolt-заведения в bbox Кройцберга
     (сетка запросов по координатам, дедупликация по slug)
  2. Для каждого ресторана из restaurants_kreuzberg.json —
     fuzzy-матчинг по названию (порог 75) + проверка расстояния (≤600м)
  3. Для совпавших — скрапит меню через Playwright
  4. Если в меню есть КБЖУ → kbju_status = "verified_wolt"
     Иначе → wolt_has_menu = true, меню сохраняется

Использование:
  python3 scrapers/kreuzberg/wolt_scanner.py
  python3 scrapers/kreuzberg/wolt_scanner.py --resume
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

# ── Пути ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent
RESTAURANTS_FILE = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
VERIFIED_FILE = ROOT / "data" / "kreuzberg" / "verified_wolt.json"
WOLT_CACHE_FILE = ROOT / "data" / "kreuzberg" / ".wolt_venues_cache.json"

# ── Константы ──────────────────────────────────────────────────────────────

BBOX = (52.4878, 13.3800, 52.5100, 13.4300)
MATCH_THRESHOLD = 75
PAUSE_S = 2.0

WOLT_SEARCH_URL = "https://consumer-api.wolt.com/v1/pages/restaurants"
WOLT_VENUE_URL = "https://wolt.com/de/deu/berlin/restaurant/{slug}"
MAX_DISTANCE_M = 600  # максимальное расстояние для матча

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

# ── Утилиты ────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


# ── Шаг 1: сбор всех Wolt-заведений в bbox ─────────────────────────────────

def fetch_wolt_venues_near(lat: float, lon: float) -> list[dict]:
    """Возвращает сырые venue-объекты из одного запроса."""
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
                v = item.get("venue") or item.get("track_id") and item
                if isinstance(v, dict) and v.get("slug"):
                    venues.append(v)
        return venues
    except Exception as e:
        print(f"    поиск ошибка ({lat:.3f},{lon:.3f}): {e}", file=sys.stderr)
        return []


def collect_all_wolt_venues() -> dict[str, dict]:
    """
    Обходит bbox сеткой точек и собирает все Wolt-заведения.
    Кэширует результат в .wolt_venues_cache.json.
    Возвращает dict slug → venue.
    """
    if WOLT_CACHE_FILE.exists():
        cached = json.loads(WOLT_CACHE_FILE.read_text())
        print(f"Wolt кэш: {len(cached)} заведений")
        return cached

    print("Собираем Wolt-заведения в bbox Кройцберга...")
    center_lat = (BBOX[0] + BBOX[2]) / 2
    lat_step = 500 / 111_000
    lon_step = 500 / (111_000 * math.cos(math.radians(center_lat)))

    all_venues: dict[str, dict] = {}
    lat = BBOX[0] + lat_step / 2
    while lat < BBOX[2]:
        lon = BBOX[1] + lon_step / 2
        while lon < BBOX[3]:
            venues = fetch_wolt_venues_near(lat, lon)
            for v in venues:
                slug = v.get("slug", "")
                if slug and slug not in all_venues:
                    all_venues[slug] = v
            time.sleep(PAUSE_S)
            lon += lon_step
        lat += lat_step

    print(f"  найдено {len(all_venues)} уникальных Wolt-заведений")
    WOLT_CACHE_FILE.write_text(json.dumps(all_venues, ensure_ascii=False, indent=2))
    return all_venues


# ── Шаг 2: fuzzy-матчинг + проверка расстояния ────────────────────────────

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в метрах."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def venue_coords(venue: dict) -> Optional[tuple[float, float]]:
    """Извлекает (lat, lon) из venue-объекта Wolt."""
    loc = venue.get("location")
    if isinstance(loc, list) and len(loc) == 2:
        return loc[1], loc[0]  # Wolt: [lon, lat]
    if isinstance(loc, dict):
        return loc.get("lat"), loc.get("lon")
    return None


NOISE_TOKENS = {
    "restaurant", "restaurants", "cafe", "café", "coffee", "imbiss",
    "berlin", "kreuzberg", "kitchen", "food", "gmbh", "und", "und",
    "the", "das", "die", "der",
}


def meaningful_tokens(name: str) -> set[str]:
    """Токены имени без общих слов-шумов."""
    return {t for t in normalize(name).split() if t not in NOISE_TOKENS and len(t) > 2}


def best_wolt_match(restaurant: dict,
                    wolt_venues: dict[str, dict],
                    used_slugs: set[str]) -> Optional[dict]:
    """
    Возвращает venue с лучшим score или None.
    Требует:
      - slug ещё не использован
      - token_set_ratio > MATCH_THRESHOLD
      - хотя бы один значимый (не шумовой) токен совпадает
      - расстояние ≤ MAX_DISTANCE_M
    """
    r_name = normalize(restaurant["name"])
    r_tokens = meaningful_tokens(restaurant["name"])
    r_lat, r_lon = restaurant["lat"], restaurant["lon"]
    best_venue = None
    best_score = 0

    for slug, venue in wolt_venues.items():
        if slug in used_slugs:
            continue

        w_name = normalize(venue.get("name", ""))
        score = fuzz.token_set_ratio(r_name, w_name)
        if score <= MATCH_THRESHOLD:
            continue

        # Требуем хотя бы один значимый общий токен
        w_tokens = meaningful_tokens(venue.get("name", ""))
        if r_tokens and w_tokens and not r_tokens & w_tokens:
            continue

        # Проверка расстояния
        coords = venue_coords(venue)
        if coords and coords[0] is not None:
            dist = haversine_m(r_lat, r_lon, coords[0], coords[1])
            if dist > MAX_DISTANCE_M:
                continue

        if score > best_score:
            best_score = score
            best_venue = venue

    return best_venue


# ── Шаг 3: меню через Playwright ───────────────────────────────────────────

def _parse_price(text: str) -> Optional[float]:
    m = re.search(r"[\d]+[.,]?\d*", text.replace(",", "."))
    return float(m.group().replace(",", ".")) if m else None


def scrape_menu_playwright(page: Page, slug: str) -> list[dict]:
    """Скрапит меню с wolt.com через Playwright."""
    url = WOLT_VENUE_URL.format(slug=slug)
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        page.wait_for_timeout(3000)

    # Закрываем баннер cookie если есть
    page.eval_on_selector_all(
        "[data-test-id='consents-banner-overlay']",
        "els => els.forEach(e => e.remove())",
    )
    page.wait_for_timeout(1000)

    items = []
    cards = page.query_selector_all("[data-test-id='horizontal-item-card']")
    for card in cards:
        try:
            name_el = card.query_selector("[data-test-id='horizontal-item-card-header']")
            desc_el = card.query_selector("p")
            price_el = card.query_selector("[data-test-id='horizontal-item-card-price']")
            img_el = card.query_selector("img")
            name = name_el.inner_text().strip() if name_el else ""
            desc = desc_el.inner_text().strip() if desc_el else ""
            price_raw = (price_el.get_attribute("aria-label") or
                         price_el.inner_text()) if price_el else ""
            price = _parse_price(price_raw)
            image_url = ""
            if img_el:
                image_url = img_el.get_attribute("src") or img_el.get_attribute("data-src") or ""
            if name:
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


# ── Main ───────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    restaurants: list[dict] = load_json(RESTAURANTS_FILE)
    verified: list[dict] = load_json(VERIFIED_FILE)
    verified_ids = {v["place_id"] for v in verified}

    # Сбор всех Wolt-заведений (один раз, с кэшем)
    wolt_venues = collect_all_wolt_venues()
    if not wolt_venues:
        print("Wolt не вернул заведений. Проверь подключение.")
        return

    stats = {"found": 0, "verified_kbju": 0, "menu_no_kbju": 0, "not_found": 0}

    # Slugи уже использованные (из предыдущего resume-прогона)
    used_slugs: set[str] = {
        r["wolt_slug"] for r in restaurants if r.get("wolt_slug")
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})

        for i, r in enumerate(restaurants, 1):
            pid = r["place_id"]

            if resume and r.get("wolt_status"):
                continue

            print(f"[{i}/{len(restaurants)}] {r['name']}", end=" ... ", flush=True)

            # Матчинг
            venue = best_wolt_match(r, wolt_venues, used_slugs)
            if venue is None:
                r["wolt_status"] = "not_found"
                stats["not_found"] += 1
                print("не найден")
                continue

            slug = venue["slug"]
            used_slugs.add(slug)
            print(f"→ {slug}", end=" ", flush=True)
            stats["found"] += 1

            # Меню через Playwright
            items = scrape_menu_playwright(page, slug)
            time.sleep(PAUSE_S)

            if not items:
                r["wolt_status"] = "no_menu"
                print("меню пустое")
                save_json(RESTAURANTS_FILE, restaurants)
                continue

            r["wolt_slug"] = slug
            # Wolt обычно не отдаёт КБЖУ в карточках — сохраняем как wolt_menu
            has_kbju = any(it.get("calories") is not None for it in items)

            if has_kbju:
                r["kbju_status"] = "verified_wolt"
                r["wolt_status"] = "verified"
                r["menu"] = items
                stats["verified_kbju"] += 1
                print(f"{len(items)} блюд + КБЖУ ✓")
                if pid not in verified_ids:
                    verified.append({
                        "place_id": pid,
                        "name": r["name"],
                        "wolt_slug": slug,
                        "items_count": len(items),
                    })
                    verified_ids.add(pid)
            else:
                r["wolt_status"] = "menu_no_kbju"
                r["wolt_has_menu"] = True
                r["wolt_menu"] = items
                stats["menu_no_kbju"] += 1
                print(f"{len(items)} блюд (без КБЖУ)")

            save_json(RESTAURANTS_FILE, restaurants)
            save_json(VERIFIED_FILE, verified)

        browser.close()

    # Итог
    print(f"\n{'─' * 50}")
    print(f"Найдено на Wolt       : {stats['found']}")
    print(f"Verified КБЖУ         : {stats['verified_kbju']}")
    print(f"Меню без КБЖУ         : {stats['menu_no_kbju']}")
    print(f"Не найдено            : {stats['not_found']}")
    print(f"Файл                  : {RESTAURANTS_FILE}")


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
