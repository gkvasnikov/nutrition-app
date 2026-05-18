#!/usr/bin/env python3
"""
Ищет рестораны из kreuzberg_north_patch на Uber Eats и скрапит меню.

Алгоритм:
  1. Запускает Chromium с playwright-stealth (обход bot detection)
  2. Устанавливает адрес доставки один раз (Friedrichshain)
  3. Для каждого ресторана без wolt_menu / ubereats_menu:
     - Поиск по имени через ubereats.com/de/berlin?q=NAME
     - Перехватывает getSearchV1 JSON-ответ
     - Fuzzy-матчинг по названию (порог 80) + расстояние ≤ 600м
     - Парсит меню из __NEXT_DATA__ (или DOM-fallback)
     - Сохраняет ubereats_menu, ubereats_url к записи
  4. Checkpoint каждые SAVE_EVERY ресторанов

Resume: пропускает рестораны у которых wolt_menu непустой
        ИЛИ выставлен ubereats_scanned.

Использование:
  python3 scrapers/kreuzberg_north_patch/ubereats_scanner.py
  python3 scrapers/kreuzberg_north_patch/ubereats_scanner.py --resume
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
from urllib.parse import quote

from dotenv import load_dotenv
from fuzzywuzzy import fuzz
from playwright.sync_api import sync_playwright, Page, Response

try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False


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
LOG_FILE         = ROOT / "logs" / "ubereats_patch.log"

# ── Константы ──────────────────────────────────────────────────────────────

MATCH_THRESHOLD  = 80
MAX_DISTANCE_M   = 600
SAVE_EVERY       = 20

# Адрес доставки закодирован прямо в URL через параметр pl (base64 JSON).
# Это надёжнее, чем вводить через UI — не требует взаимодействия с формой.
# Friedrichshain: Warschauer Str. 10, Berlin (52.5074, 13.4515)
PL_PARAM = (
    "eyJhZGRyZXNzIjp7ImFkZHJlc3MxIjoiV2Fyc2NoYXVlciBTdHIuIDEwLCBC"
    "ZXJsaW4iLCJjaXR5IjoiQmVybGluIiwiY291bnRyeSI6IkRFIiwicG9zdGFsQ2"
    "9kZSI6IjEwMjQzIiwicmVnaW9uIjoiIn0sImxhdGl0dWRlIjo1Mi41MDc0LCJs"
    "b25naXR1ZGUiOjEzLjQ1MTV9"
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

NOISE_TOKENS = {
    "restaurant", "restaurants", "cafe", "café", "coffee", "imbiss",
    "berlin", "kreuzberg", "friedrichshain", "mitte", "kitchen", "food",
    "gmbh", "und", "the", "das", "die", "der", "am", "im", "bistro",
}

# ── Логгер ─────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── I/O ────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


# ── Утилиты матчинга ────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def meaningful_tokens(name: str) -> set[str]:
    return {t for t in normalize(name).split() if t not in NOISE_TOKENS and len(t) > 2}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Извлечение заведений из ответа поиска ──────────────────────────────────

def _extract_store(obj: dict) -> Optional[dict]:
    """Извлекает {name, slug, url, lat, lon} из объекта store Uber Eats."""
    title = ""
    if isinstance(obj.get("title"), dict):
        title = obj["title"].get("text", "")
    elif isinstance(obj.get("title"), str):
        title = obj["title"]
    if not title:
        return None

    action = obj.get("actionUrl", "") or obj.get("href", "")
    m = re.search(r"/store/([^/?#]+)/([^/?#]+)", action)
    if not m:
        return None
    slug, ue_id = m.group(1), m.group(2)
    url = f"https://www.ubereats.com/de/store/{slug}/{ue_id}"

    marker = obj.get("mapMarker") or {}
    lat = marker.get("latitude")
    lon = marker.get("longitude")
    return {"name": title, "slug": slug, "url": url, "lat": lat, "lon": lon}


def venues_from_search(payload: dict) -> list[dict]:
    """Извлекает список заведений из ответа getSearchV1."""
    results: list[dict] = []
    seen: set[str] = set()

    def _walk(obj):
        if isinstance(obj, list):
            for item in obj:
                _walk(item)
            return
        if not isinstance(obj, dict):
            return
        t = obj.get("type", "")
        if t in ("REGULAR_STORE", "SEARCH_RESULT"):
            store = obj.get("store") or obj
            v = _extract_store(store)
            if v and v["slug"] not in seen:
                seen.add(v["slug"])
                results.append(v)
        else:
            for v in obj.values():
                _walk(v)

    data = payload.get("data") or payload
    _walk(data)
    return results


# ── Fuzzy-матчинг ──────────────────────────────────────────────────────────

def best_match(restaurant: dict,
               candidates: list[dict],
               used_slugs: set[str]) -> Optional[dict]:
    r_name   = normalize(restaurant["name"])
    r_tokens = meaningful_tokens(restaurant["name"])
    r_lat    = restaurant.get("lat")
    r_lon    = restaurant.get("lon")

    best_v, best_score = None, 0
    for v in candidates:
        if v["slug"] in used_slugs:
            continue
        score = fuzz.token_set_ratio(r_name, normalize(v["name"]))
        if score < MATCH_THRESHOLD:
            continue
        w_tokens = meaningful_tokens(v["name"])
        if r_tokens and w_tokens and not (r_tokens & w_tokens):
            continue
        if r_lat and r_lon and v.get("lat") and v.get("lon"):
            dist = haversine_m(r_lat, r_lon, float(v["lat"]), float(v["lon"]))
            if dist > MAX_DISTANCE_M:
                continue
        if score > best_score:
            best_score, best_v = score, v

    return best_v


# ── Браузер: настройка адреса ───────────────────────────────────────────────

def setup_delivery_address(page: Page) -> bool:
    """Открывает Uber Eats, устанавливает адрес доставки. True = успех."""
    try:
        page.goto("https://www.ubereats.com/de/berlin",
                  wait_until="domcontentloaded", timeout=40_000)
        page.wait_for_timeout(random.randint(2500, 3500))

        # Ищем поле ввода адреса
        inp = None
        for sel in [
            "#location-typeahead-home-input",
            "input[placeholder*='Adresse']",
            "input[placeholder*='address']",
            "input[placeholder*='Lieferadresse']",
        ]:
            inp = page.query_selector(sel)
            if inp:
                break

        if not inp:
            # Кликаем по кнопке «Liefern» если адрес ещё не введён
            for sel in ["[data-testid='home-delivery-btn']", "button:has-text('Liefern')"]:
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    page.wait_for_timeout(1500)
                    inp = page.query_selector("input[placeholder*='Adresse'], input[placeholder*='address']")
                    if inp:
                        break

        if not inp:
            log.warning("Address input not found — proceeding without address")
            return False

        inp.click()
        page.wait_for_timeout(random.randint(400, 700))
        inp.type(DELIVERY_ADDRESS, delay=random.randint(50, 90))
        page.wait_for_timeout(random.randint(2000, 3000))

        # Выбираем первую подсказку
        first = page.query_selector("[role='option']")
        if first:
            first.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_timeout(random.randint(3000, 4000))
        print(f"  Адрес установлен: {DELIVERY_ADDRESS}")
        log.info("Delivery address set: %s", DELIVERY_ADDRESS)
        return True

    except Exception as e:
        log.warning("Address setup failed: %s", e)
        print(f"  Предупреждение: адрес не установлен ({e})")
        return False


# ── Поиск ресторана ─────────────────────────────────────────────────────────

def search_on_ubereats(page: Page, name: str) -> list[dict]:
    """
    Ищет ресторан по имени.
    Адрес доставки передаётся через pl-параметр (base64 JSON).
    Перехватывает все JSON-ответы ubereats.com, ищет store-объекты.
    """
    captured: list[dict] = []

    def handle_resp(resp: Response):
        try:
            if resp.status == 200 and "ubereats.com" in resp.url \
                    and "application/json" in resp.headers.get("content-type", ""):
                data = resp.json()
                if isinstance(data, dict):
                    captured.append(data)
        except Exception:
            pass

    page.on("response", handle_resp)
    try:
        url = f"https://www.ubereats.com/de/berlin?q={quote(name)}&pl={PL_PARAM}"
        page.goto(url, wait_until="domcontentloaded", timeout=35_000)
        page.wait_for_timeout(random.randint(2500, 3500))
    except Exception as e:
        log.warning("Search navigation error for '%s': %s", name, e)
    finally:
        page.remove_listener("response", handle_resp)

    venues: list[dict] = []
    seen: set[str] = set()
    for payload in captured:
        for v in venues_from_search(payload):
            if v["slug"] not in seen:
                seen.add(v["slug"])
                venues.append(v)

    # DOM fallback: собираем ссылки /de/store/... со страницы
    if not venues:
        links = page.eval_on_selector_all(
            "a[href*='/de/store/']",
            """els => els.map(a => ({
                href: a.href,
                name: (a.querySelector('h3,h4,[data-testid]') || a).innerText.trim().split('\\n')[0]
            }))"""
        )
        for lnk in links:
            m = re.search(r"/de/store/([^/?#]+)/([^/?#]+)", lnk.get("href", ""))
            if m:
                slug, ue_id = m.group(1), m.group(2)
                if slug not in seen:
                    seen.add(slug)
                    venues.append({
                        "name": lnk.get("name", slug),
                        "slug": slug,
                        "url": f"https://www.ubereats.com/de/store/{slug}/{ue_id}",
                        "lat": None,
                        "lon": None,
                    })

    return venues


# ── Парсинг меню ────────────────────────────────────────────────────────────

def parse_menu(page: Page, url: str) -> list[dict]:
    """Парсит меню ресторана. Приоритет: __NEXT_DATA__. Fallback: DOM."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35_000)
    except Exception:
        pass
    page.wait_for_timeout(random.randint(2000, 3000))

    # Докрутим для подгрузки ленивого контента
    for _ in range(4):
        page.keyboard.press("End")
        page.wait_for_timeout(600)

    # ── __NEXT_DATA__ ────────────────────────────────────────────────────────
    items = page.evaluate("""() => {
        try {
            const el = document.getElementById('__NEXT_DATA__');
            if (!el) return [];
            const data = JSON.parse(el.textContent);
            const results = [];
            const seen = new Set();

            function walk(obj, category) {
                if (Array.isArray(obj)) {
                    obj.forEach(o => walk(o, category));
                    return;
                }
                if (typeof obj !== 'object' || !obj) return;

                // Секция меню → запоминаем категорию
                const cat = obj.title && typeof obj.title === 'string' && obj.catalogSectionItems
                    ? obj.title : category;

                // Позиция меню
                if (obj.title && typeof obj.title === 'string' && obj.title.length > 1
                        && (obj.price !== undefined || obj.itemDescription !== undefined)) {
                    const key = obj.title;
                    if (!seen.has(key)) {
                        seen.add(key);
                        results.push({
                            name:        obj.title,
                            description: (obj.itemDescription || obj.description || '').substring(0, 250),
                            price:       obj.price != null ? Math.round(obj.price) / 100 : null,
                            category:    cat || '',
                            calories:    null, protein: null, fat: null, carbs: null,
                        });
                    }
                    return;
                }
                Object.values(obj).forEach(v => walk(v, cat));
            }
            walk(data, '');
            return results;
        } catch(e) { return []; }
    }""")

    # ── DOM fallback ─────────────────────────────────────────────────────────
    if not items:
        items = page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            let currentCat = '';
            const nodes = document.querySelectorAll('h2, h3, h4, li[data-testid]');
            nodes.forEach(el => {
                const tag = el.tagName.toLowerCase();
                if (tag === 'h2') { currentCat = el.textContent.trim(); return; }
                const name = el.querySelector('h3, h4, [data-testid*="title"]');
                const nameText = (name || el).textContent.trim().split('\\n')[0];
                if (!nameText || nameText.length < 2 || nameText.length > 120 || seen.has(nameText)) return;
                seen.add(nameText);
                const parent = el.closest('li, [data-testid]') || el.parentElement;
                const desc   = parent ? (parent.querySelector('p') || {}).textContent || '' : '';
                const priceEl = parent ? parent.querySelector('[data-testid*="price"], [class*="price"]') : null;
                const priceText = priceEl ? priceEl.textContent : '';
                const priceM = priceText.match(/[\d]+[,.]?\d*/);
                results.push({
                    name:        nameText,
                    description: desc.trim().substring(0, 250),
                    price:       priceM ? parseFloat(priceM[0].replace(',', '.')) : null,
                    category:    currentCat,
                    calories: null, protein: null, fat: null, carbs: null,
                });
            });
            return results;
        }""")

    return items or []


# ── Main ───────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    restaurants: list[dict] = load_json(RESTAURANTS_FILE)
    if not restaurants:
        print(f"Файл не найден или пустой: {RESTAURANTS_FILE}")
        return

    # Считаем сколько осталось обработать
    to_process = [
        r for r in restaurants
        if not r.get("wolt_menu")          # у кого нет Wolt-меню
        and not r.get("ubereats_scanned")  # и ещё не обработан этим скриптом
    ]
    print(f"Ресторанов всего         : {len(restaurants)}")
    print(f"С Wolt-меню (пропускаем) : {sum(1 for r in restaurants if r.get('wolt_menu'))}")
    print(f"К обработке Uber Eats    : {len(to_process)}")
    if HAS_STEALTH:
        print("Stealth: включён ✓")
    else:
        print("Stealth: не установлен (pip install playwright-stealth)")

    if not to_process:
        print("Нечего обрабатывать.")
        return

    found       = 0
    not_found   = 0
    total_dishes = 0
    saved_count  = 0
    used_slugs: set[str] = {r.get("ubereats_slug", "") for r in restaurants if r.get("ubereats_slug")}
    top: list[tuple[str, int]] = []   # (name, dishes_count) для топ-5

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        if HAS_STEALTH:
            Stealth().use_sync(page)

        # Адрес закодирован в pl-параметре каждого search URL — UI не нужен
        print(f"  Адрес доставки: Warschauer Str. 10, Berlin (через pl-параметр)")

        total = len(to_process)
        for i, r in enumerate(to_process, 1):
            name = r.get("name", "?")
            print(f"[{i}/{total}] {name[:50]}", end=" … ", flush=True)

            # Поиск
            candidates = search_on_ubereats(page, name)
            match = best_match(r, candidates, used_slugs) if candidates else None

            if match is None:
                r["ubereats_menu"]    = []
                r["ubereats_scanned"] = True
                not_found += 1
                print(f"не найден (кандидатов: {len(candidates)})")
                log.info("NOT_FOUND | %s | candidates=%d", name, len(candidates))
            else:
                used_slugs.add(match["slug"])
                r["ubereats_slug"] = match["slug"]
                r["ubereats_url"]  = match["url"]
                print(f"→ {match['name'][:40]}", end=" ", flush=True)

                # Парсим меню
                items = parse_menu(page, match["url"])
                r["ubereats_menu"]    = items
                r["ubereats_scanned"] = True

                if items:
                    found        += 1
                    total_dishes += len(items)
                    top.append((name, len(items)))
                    print(f"{len(items)} блюд ✓")
                    log.info("FOUND | %s | slug=%s | dishes=%d", name, match["slug"], len(items))
                else:
                    not_found += 1
                    print("меню пустое")
                    log.warning("EMPTY_MENU | %s | slug=%s", name, match["slug"])

            # Checkpoint
            saved_count += 1
            if saved_count % SAVE_EVERY == 0:
                save_json(RESTAURANTS_FILE, restaurants)
                print(f"  ── checkpoint ({saved_count}/{total}) ──")

            time.sleep(random.uniform(2.0, 3.0))

        browser.close()

    # Финальное сохранение
    save_json(RESTAURANTS_FILE, restaurants)

    # Топ-5
    top.sort(key=lambda x: x[1], reverse=True)

    print(f"\n{'─' * 52}")
    print(f"Ресторанов найдено на Uber Eats : {found} / {found + not_found}")
    print(f"Всего блюд собрано              : {total_dishes}")
    if top:
        print(f"\nТоп-5 ресторанов по блюдам:")
        for j, (rname, cnt) in enumerate(top[:5], 1):
            print(f"  {j}. {rname[:45]:<45} {cnt} блюд")
    print(f"\nФайл : {RESTAURANTS_FILE}")
    print(f"Лог  : {LOG_FILE}")
    log.info("DONE | found=%d not_found=%d dishes=%d", found, not_found, total_dishes)


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
