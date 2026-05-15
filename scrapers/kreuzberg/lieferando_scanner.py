"""
Ищет рестораны Кройцберга на Lieferando через Playwright + stealth.
Обрабатывает только рестораны с kbju_status = "no_data".

Алгоритм:
  1. Собирает slug'и ресторанов по PLZ Кройцберга (listing pages)
  2. Для каждого slug'а скрапит меню (scroll до конца)
  3. Fuzzy-матчинг с restaurants_kreuzberg.json по названию
  4. verified_lieferando если есть КБЖУ, иначе lieferando_has_menu=true

Использование:
  python3 scrapers/kreuzberg/lieferando_scanner.py
  python3 scrapers/kreuzberg/lieferando_scanner.py --resume
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fuzzywuzzy import fuzz
from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth

load_dotenv()

# ── Пути ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent
RESTAURANTS_FILE = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
VERIFIED_FILE = ROOT / "data" / "kreuzberg" / "verified_lieferando.json"
SLUGS_CACHE = ROOT / "data" / "kreuzberg" / ".lieferando_slugs_cache.json"

# ── Константы ──────────────────────────────────────────────────────────────

KREUZBERG_PLZS = ["10961", "10963", "10965", "10967", "10969", "10997", "10999"]
LISTING_URL = "https://www.lieferando.de/lieferservice/essen/{plz}"
MENU_URL = "https://www.lieferando.de{slug}"

MATCH_THRESHOLD = 75
PAUSE_S = 2.0

NOISE_TOKENS = {
    "restaurant", "restaurants", "cafe", "café", "coffee", "imbiss",
    "kitchen", "food", "gmbh", "und", "the", "das", "die", "der",
    "bistro", "gasthaus", "gaststaette", "gaststatte",
    "fruhstuckshaus", "frühstückshaus", "pizzeria", "trattoria",
    "osteria", "ristorante", "brasserie",
    "berlin", "kreuzberg", "neukolln", "neukölln", "mitte",
    "checkpoint", "charlie", "potsdamer", "platz",
    "strasse", "straße", "damm", "ufer", "kiez",
    "indisches", "indisch", "chinesisch", "vietnamesisch",
    "italienisch", "griechisch", "türkisch", "arabisch",
    "korean", "thai", "sushi", "burger", "doener", "döner",
}

# ── Утилиты ────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def meaningful_tokens(name: str) -> set[str]:
    return {t for t in normalize(name).split() if t not in NOISE_TOKENS and len(t) > 2}


# ── Шаг 1: сбор slug'ов с листинг-страниц ──────────────────────────────────

def scroll_to_load(page: Page) -> None:
    prev_count = 0
    for _ in range(25):
        page.keyboard.press("End")
        time.sleep(1.5)
        count = len(page.query_selector_all("[data-test-id='restaurant-card-wrapper']"))
        if count == prev_count and count > 0:
            break
        prev_count = count


def slug_to_name(slug: str) -> str:
    """Derives a human-readable name from the Lieferando slug path."""
    path = slug.split("?")[0]  # strip query params
    path = path.replace("/speisekarte/", "")
    # Remove trailing digits (e.g. -2, -3)
    path = re.sub(r"-\d+$", "", path)
    return path.replace("-", " ").strip()


def collect_slugs_for_plz(page: Page, plz: str) -> list[dict]:
    url = LISTING_URL.format(plz=plz)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(3)
        scroll_to_load(page)
    except Exception as e:
        print(f"  PLZ {plz} ошибка загрузки: {e}", file=sys.stderr)
        return []

    entries = []
    seen = set()
    cards = page.query_selector_all("a[href*='/speisekarte/']")
    for card in cards:
        href = card.get_attribute("href") or ""
        # Normalise slug: strip query params for dedup key
        clean_slug = href.split("?")[0]
        if not clean_slug or clean_slug in seen:
            continue
        seen.add(clean_slug)
        # Try DOM selectors first, fall back to slug-derived name
        name_el = card.query_selector("h2, h3, [data-test-id='restaurant-name'], [class*='name'], [class*='title']")
        name = name_el.inner_text().strip() if name_el else ""
        if not name:
            name = slug_to_name(clean_slug)
        entries.append({"slug": clean_slug, "name": name, "plz": plz})
    return entries


def collect_all_slugs(page: Page) -> list[dict]:
    if SLUGS_CACHE.exists():
        cached = json.loads(SLUGS_CACHE.read_text())
        print(f"Lieferando slug-кэш: {len(cached)} заведений")
        return cached

    print("Собираем Lieferando slug'и по PLZ Кройцберга...")
    all_entries: list[dict] = []
    seen_slugs: set[str] = set()

    for plz in KREUZBERG_PLZS:
        print(f"  PLZ {plz}...", end=" ", flush=True)
        entries = collect_slugs_for_plz(page, plz)
        new = [e for e in entries if e["slug"] not in seen_slugs]
        for e in new:
            seen_slugs.add(e["slug"])
        all_entries.extend(new)
        print(f"{len(new)} новых")
        time.sleep(PAUSE_S)

    print(f"  Итого: {len(all_entries)} уникальных Lieferando-заведений")
    SLUGS_CACHE.write_text(json.dumps(all_entries, ensure_ascii=False, indent=2))
    return all_entries


# ── Шаг 2: scrape меню ─────────────────────────────────────────────────────

def parse_price(text: str) -> Optional[float]:
    m = re.search(r"[\d]+[.,]?\d*", text.replace(",", "."))
    return float(m.group().replace(",", ".")) if m else None


def scrape_menu(page: Page, slug: str) -> list[dict]:
    url = MENU_URL.format(slug=slug)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=35000)
        time.sleep(2)
    except Exception:
        time.sleep(3)

    # Lazy-load: скроллим до стабилизации
    prev = 0
    for _ in range(20):
        page.keyboard.press("End")
        time.sleep(1.2)
        count = len(page.query_selector_all("[data-item-id]"))
        if count == prev and count > 0:
            break
        prev = count

    items = []
    cards = page.query_selector_all("[data-item-id]")
    for card in cards:
        try:
            name_el = card.query_selector("[data-qa='product-name'], h3, h4")
            desc_el = card.query_selector("[data-qa='product-description'], p")
            price_el = card.query_selector("[data-qa='product-price'], [class*='price']")
            name = name_el.inner_text().strip() if name_el else ""
            desc = desc_el.inner_text().strip() if desc_el else ""
            price_raw = price_el.inner_text().strip() if price_el else ""
            price = parse_price(price_raw) if price_raw else None

            # КБЖУ — Lieferando иногда показывает прямо в карточке
            kcal = _extract_kcal(card)

            if name:
                items.append({
                    "name": name,
                    "description": desc,
                    "price": price,
                    "calories": kcal,
                    "protein": None,
                    "fat": None,
                    "carbs": None,
                })
        except Exception:
            continue
    return items


def _extract_kcal(card) -> Optional[float]:
    """Пытается найти калорийность в тексте карточки."""
    try:
        text = card.inner_text()
        m = re.search(r"(\d+)\s*(?:kcal|kkal|cal)\b", text, re.I)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


# ── Шаг 3: fuzzy-матчинг ───────────────────────────────────────────────────

def best_match(lieferando_entry: dict,
               restaurants: list[dict],
               used_slugs: set[str]) -> Optional[dict]:
    slug = lieferando_entry["slug"]
    if slug in used_slugs:
        return None

    lf_name = normalize(lieferando_entry.get("name", ""))
    lf_tokens = meaningful_tokens(lieferando_entry.get("name", ""))

    best_r = None
    best_score = 0

    for r in restaurants:
        if r.get("kbju_status") != "no_data":
            continue
        if r.get("lieferando_status"):
            continue

        r_name = normalize(r["name"])
        score = fuzz.token_set_ratio(lf_name, r_name)
        if score <= MATCH_THRESHOLD:
            continue

        r_tokens = meaningful_tokens(r["name"])
        if lf_tokens and r_tokens and not lf_tokens & r_tokens:
            continue

        if score > best_score:
            best_score = score
            best_r = r

    return best_r


# ── Main ───────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    restaurants: list[dict] = load_json(RESTAURANTS_FILE)
    verified: list[dict] = load_json(VERIFIED_FILE)
    verified_ids = {v["place_id"] for v in verified}

    targets = [r for r in restaurants if r.get("kbju_status") == "no_data"]
    print(f"Целевых ресторанов (kbju_status=no_data): {len(targets)}")

    used_slugs: set[str] = {
        r["lieferando_slug"] for r in restaurants if r.get("lieferando_slug")
    }

    stats = {"found": 0, "verified_kbju": 0, "menu_no_kbju": 0, "not_found": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 12; Pixel 5) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            locale="de-DE",
        )
        page = ctx.new_page()
        Stealth().use_sync(page)

        # Сбор slug'ов
        lieferando_entries = collect_all_slugs(page)
        if not lieferando_entries:
            print("Не удалось собрать slug'и. Проверь соединение.")
            browser.close()
            return

        print(f"\nМатчинг {len(lieferando_entries)} Lieferando-заведений...")

        for i, entry in enumerate(lieferando_entries, 1):
            slug = entry["slug"]

            if resume and slug in used_slugs:
                continue

            r = best_match(entry, restaurants, used_slugs)
            if r is None:
                continue

            pid = r["place_id"]
            print(f"[{i}/{len(lieferando_entries)}] {entry['name']} → {r['name']}", end=" ", flush=True)
            stats["found"] += 1
            used_slugs.add(slug)
            r["lieferando_slug"] = slug

            # Меню
            items = scrape_menu(page, slug)
            time.sleep(PAUSE_S)

            if not items:
                r["lieferando_status"] = "no_menu"
                print("меню пустое")
                save_json(RESTAURANTS_FILE, restaurants)
                continue

            has_kbju = any(it.get("calories") is not None for it in items)

            if has_kbju:
                r["kbju_status"] = "verified_lieferando"
                r["lieferando_status"] = "verified"
                r["menu"] = items
                stats["verified_kbju"] += 1
                print(f"{len(items)} блюд + КБЖУ ✓")
                if pid not in verified_ids:
                    verified.append({
                        "place_id": pid,
                        "name": r["name"],
                        "lieferando_slug": slug,
                        "items_count": len(items),
                    })
                    verified_ids.add(pid)
            else:
                r["lieferando_status"] = "menu_no_kbju"
                r["lieferando_has_menu"] = True
                r["lieferando_menu"] = items
                stats["menu_no_kbju"] += 1
                print(f"{len(items)} блюд (без КБЖУ)")

            save_json(RESTAURANTS_FILE, restaurants)
            save_json(VERIFIED_FILE, verified)

        browser.close()

    print(f"\n{'─' * 50}")
    print(f"Целевых ресторанов    : {len(targets)}")
    print(f"Найдено на Lieferando : {stats['found']}")
    print(f"Verified КБЖУ         : {stats['verified_kbju']}")
    print(f"Меню без КБЖУ         : {stats['menu_no_kbju']}")
    print(f"Файл                  : {RESTAURANTS_FILE}")


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
