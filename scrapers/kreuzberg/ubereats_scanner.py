"""
Собирает рестораны Кройцберга с Uber Eats через WebKit Playwright.

Алгоритм:
  1. Вводит адрес Кройцберга → Uber Eats устанавливает зону доставки
  2. Скроллит страницу, перехватывает все getFeedV1 ответы
  3. Извлекает рестораны из feedItems (REGULAR_STORE + REGULAR_CAROUSEL)
  4. Fuzzy-матчинг по названию (>80) с restaurants_kreuzberg.json
  5. Открывает страницу каждого ресторана, парсит меню из DOM/__NEXT_DATA__

Использование:
  python3 scrapers/kreuzberg/ubereats_scanner.py
  python3 scrapers/kreuzberg/ubereats_scanner.py --resume
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import sys
from pathlib import Path
from typing import Optional

from fuzzywuzzy import fuzz

ROOT = Path(__file__).parent.parent.parent

from playwright.async_api import async_playwright, Page, Response

RESTAURANTS_FILE = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
CACHE_FILE       = ROOT / "data" / "kreuzberg" / ".ubereats_cache.json"
ERRORS_LOG       = ROOT / "errors.log"

logging.basicConfig(
    filename=str(ERRORS_LOG),
    level=logging.ERROR,
    format="%(asctime)s  ubereats_scanner  %(message)s",
)

MATCH_THRESHOLD = 80
MAX_DISTANCE_M  = 600
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)
DELIVERY_ADDRESS = "Bergmannstraße 1 Berlin"
NOISE_TOKENS = {
    "restaurant", "cafe", "café", "berlin", "kreuzberg", "kitchen",
    "food", "gmbh", "und", "the", "das", "die", "der", "bistro",
}


# ── Утилиты ──────────────────────────────────────────────────────────────────

def load_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else None

def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()

def meaningful_tokens(name: str) -> set[str]:
    return {t for t in normalize(name).split() if t not in NOISE_TOKENS and len(t) > 2}

async def rand_delay(page: Page, lo=800, hi=2000):
    await page.wait_for_timeout(random.randint(lo, hi))


# ── Шаг 1: сбор заведений через getFeedV1 ────────────────────────────────────

def _extract_store(store: dict) -> Optional[dict]:
    """Извлекает данные заведения из объекта store."""
    title = (store.get("title") or {}).get("text", "")
    if not title:
        return None
    action = store.get("actionUrl", "")
    m = re.search(r"/store/([^/]+)/([^/?]+)", action)
    if not m:
        return None
    slug, ue_id = m.group(1), m.group(2)
    url = f"https://www.ubereats.com/de/store/{slug}/{ue_id}"

    # Координаты из mapMarker
    marker = store.get("mapMarker") or {}
    lat = marker.get("latitude")
    lon = marker.get("longitude")

    return {"name": title, "slug": slug, "ue_id": ue_id, "url": url, "lat": lat, "lon": lon}


def extract_venues_from_feed(payload: dict) -> list[dict]:
    venues = []
    seen = set()
    feed = (payload.get("data") or {}).get("feedItems", [])
    for item in feed:
        t = item.get("type")
        if t == "REGULAR_STORE":
            v = _extract_store(item.get("store") or {})
            if v and v["slug"] not in seen:
                seen.add(v["slug"])
                venues.append(v)
        elif t == "REGULAR_CAROUSEL":
            for s in (item.get("carousel") or {}).get("stores", []):
                v = _extract_store(s)
                if v and v["slug"] not in seen:
                    seen.add(v["slug"])
                    venues.append(v)
    return venues


async def collect_venues_with_address(page: Page) -> list[dict]:
    """Вводит адрес, перехватывает getFeedV1, скроллит для получения всех ресторанов."""
    all_venues: dict[str, dict] = {}
    captured: list = []

    async def on_resp(resp: Response):
        try:
            if "getFeedV1" in resp.url and resp.status == 200:
                body = await resp.json()
                captured.append(body)
                for v in extract_venues_from_feed(body):
                    if v["slug"] not in all_venues:
                        all_venues[v["slug"]] = v
        except Exception:
            pass

    page.on("response", on_resp)

    print("Загружаем ubereats.com/de ...")
    await page.goto("https://www.ubereats.com/de", wait_until="networkidle", timeout=40000)
    await rand_delay(page, 2000, 3000)

    # Вводим адрес
    inp = await page.query_selector("#location-typeahead-home-input")
    if not inp:
        # Пробуем другие селекторы
        for sel in ["input[placeholder*='Adresse']", "input[placeholder*='address']", "input[type='text']"]:
            inp = await page.query_selector(sel)
            if inp:
                break
    if not inp:
        print("Поле ввода адреса не найдено!")
        return []

    await inp.click()
    await rand_delay(page, 500, 800)
    await inp.type(DELIVERY_ADDRESS, delay=70)
    await rand_delay(page, 2500, 3500)

    # Кликаем первую подсказку
    first = await page.query_selector("[role='option']")
    if first:
        await first.click()
    else:
        await page.keyboard.press("Enter")

    await rand_delay(page, 4000, 5000)
    print(f"  После ввода адреса: {len(all_venues)} заведений")

    # Скроллим для подгрузки
    prev = 0
    stale = 0
    for i in range(50):
        await page.keyboard.press("End")
        await rand_delay(page, 1000, 1600)
        if len(all_venues) == prev:
            stale += 1
            if stale >= 6:
                break
        else:
            stale = 0
            prev = len(all_venues)
        if i % 10 == 0 and i > 0:
            print(f"  Скролл {i}: {len(all_venues)} заведений")

    page.remove_listener("response", on_resp)
    print(f"  Итого: {len(all_venues)} заведений (из {len(captured)} getFeedV1 ответов)")
    return list(all_venues.values())


# ── Шаг 2: матчинг ───────────────────────────────────────────────────────────

def best_match(restaurant: dict, ue_venues: list[dict], used: set[str]) -> Optional[dict]:
    r_name   = normalize(restaurant["name"])
    r_tokens = meaningful_tokens(restaurant["name"])
    r_lat    = restaurant.get("lat")
    r_lon    = restaurant.get("lon")

    best_v, best_score = None, 0
    for v in ue_venues:
        if v["slug"] in used:
            continue
        score = fuzz.token_set_ratio(r_name, normalize(v["name"]))
        if score < MATCH_THRESHOLD:
            continue
        v_tokens = meaningful_tokens(v["name"])
        if r_tokens and v_tokens and not r_tokens & v_tokens:
            continue
        if r_lat and r_lon and v.get("lat") and v.get("lon"):
            dist = haversine_m(r_lat, r_lon, v["lat"], v["lon"])
            if dist > MAX_DISTANCE_M:
                continue
        if score > best_score:
            best_score, best_v = score, v

    return best_v


# ── Шаг 3: парсинг меню ──────────────────────────────────────────────────────

async def scrape_menu(page: Page, url: str) -> list[dict]:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
    except Exception:
        return []

    await rand_delay(page, 2000, 3000)
    for _ in range(5):
        await page.keyboard.press("End")
        await rand_delay(page, 700, 1200)

    # Приоритет: __NEXT_DATA__ JSON
    items = await page.evaluate("""() => {
        try {
            const el = document.getElementById('__NEXT_DATA__');
            if (!el) return [];
            const data = JSON.parse(el.textContent);
            const results = [];
            const seen = new Set();
            function walk(obj) {
                if (Array.isArray(obj)) { obj.forEach(walk); return; }
                if (typeof obj !== 'object' || !obj) return;
                if (obj.title && typeof obj.title === 'string' && obj.title.length > 1 &&
                    (obj.price !== undefined || obj.itemDescription !== undefined || obj.description !== undefined)) {
                    if (!seen.has(obj.title)) {
                        seen.add(obj.title);
                        results.push({
                            name: obj.title,
                            description: (obj.itemDescription || obj.description || '').substring(0, 200),
                            price: obj.price ? Math.round(obj.price) / 100 : null,
                        });
                    }
                    return;
                }
                Object.values(obj).forEach(walk);
            }
            walk(data);
            return results;
        } catch(e) { return []; }
    }""")

    # Fallback: DOM
    if not items:
        items = await page.evaluate("""() => {
            const results = [];
            const seen = new Set();
            document.querySelectorAll('h3, h4').forEach(el => {
                const name = el.textContent.trim();
                if (name.length < 2 || name.length > 100 || seen.has(name)) return;
                seen.add(name);
                const parent = el.closest('li, [data-testid]') || el.parentElement;
                const desc = parent ? (parent.querySelector('p') || {}).textContent || '' : '';
                results.push({name, description: desc.trim().substring(0,200), price: null});
            });
            return results;
        }""")

    return items


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(resume: bool) -> None:
    restaurants: list[dict] = json.loads(RESTAURANTS_FILE.read_text())

    async with async_playwright() as p:
        browser = await p.webkit.launch(headless=True)
        context = await browser.new_context(
            user_agent=UA, locale="de-DE",
            timezone_id="Europe/Berlin",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        # ── Получаем/загружаем кэш заведений ─────────────────────────────────
        cached = load_json(CACHE_FILE)
        if cached:
            ue_venues = cached
            print(f"Кэш: {len(ue_venues)} заведений Uber Eats")
        else:
            ue_venues = await collect_venues_with_address(page)
            if not ue_venues:
                print("Не удалось собрать заведения.")
                await browser.close()
                return
            save_json(CACHE_FILE, ue_venues)
            print(f"Кэш сохранён: {len(ue_venues)} заведений")

        # ── Матчинг и скрапинг меню ───────────────────────────────────────────
        used_slugs: set[str] = set()
        total = len(restaurants)
        ok = skipped = not_found = errors = 0
        total_dishes = 0

        for i, r in enumerate(restaurants, 1):
            if resume and r.get("ubereats_status"):
                skipped += 1
                continue

            match = best_match(r, ue_venues, used_slugs)
            if not match:
                r["ubereats_status"] = "not_found"
                not_found += 1
                continue

            used_slugs.add(match["slug"])
            print(f"[{i}/{total}] {r['name'][:35]} → {match['name'][:35]}", end=" ", flush=True)

            try:
                items = await scrape_menu(page, match["url"])
                if not items:
                    print("нет блюд")
                    r["ubereats_status"] = "no_menu"
                else:
                    print(f"{len(items)} блюд ✓")
                    r["ubereats_menu"] = items
                    r["ubereats_status"] = "verified"
                    r["ubereats_url"]   = match["url"]
                    ok += 1
                    total_dishes += len(items)
            except Exception as e:
                print(f"ОШИБКА: {e}")
                logging.error("%s | %s", r["name"], e)
                r["ubereats_status"] = "error"
                errors += 1

            save_json(RESTAURANTS_FILE, restaurants)
            await rand_delay(page, 2000, 4000)

        await browser.close()

    print(f"\n{'─'*50}")
    print(f"Успешно меню   : {ok} ({total_dishes} блюд)")
    print(f"Не найдено     : {not_found}")
    print(f"Ошибок         : {errors}")
    print(f"Пропущено      : {skipped}")


if __name__ == "__main__":
    asyncio.run(run(resume="--resume" in sys.argv))
