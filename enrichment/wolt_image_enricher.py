#!/usr/bin/env python3
"""
Добавляет image_url к существующим блюдам в wolt_menu.
НЕ трогает нутриенты — только image_url.

Алгоритм:
  1. Читает all_restaurants.json
  2. Для каждого ресторана с wolt_slug и wolt_menu открывает Wolt-страницу
  3. Извлекает пары {name → image_url} из карточек блюд
  4. Матчит по имени к существующим блюдам и дописывает image_url
  5. Сохраняет (атомарно)

Запуск:
  python3 enrichment/wolt_image_enricher.py
  python3 enrichment/wolt_image_enricher.py --limit 10   # первые 10 ресторанов (тест)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "all_restaurants.json"
BACKUP_FILE = ROOT / "data" / "all_restaurants_backup_wolt_images.json"
WOLT_VENUE_URL = "https://wolt.com/de/deu/berlin/restaurant/{slug}"

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def _dismiss_banner(page: Page) -> None:
    page.eval_on_selector_all(
        "[data-test-id='consents-banner-overlay']",
        "els => els.forEach(e => e.remove())",
    )


def _normalize(s: str) -> str:
    """Нормализует имя блюда для матчинга."""
    return re.sub(r"\s+", " ", s.lower().strip())


def scrape_images(page: Page, slug: str) -> dict[str, str]:
    """Возвращает {normalized_name: image_url} для всех блюд на странице."""
    url = WOLT_VENUE_URL.format(slug=slug)
    try:
        page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception:
        page.wait_for_timeout(3000)

    _dismiss_banner(page)
    page.wait_for_timeout(500)

    images: dict[str, str] = {}
    cards = page.query_selector_all("[data-test-id='horizontal-item-card']")
    for card in cards:
        try:
            name_el = card.query_selector("[data-test-id='horizontal-item-card-header']")
            img_el  = card.query_selector("img")
            if not name_el:
                continue
            name = name_el.inner_text().strip()
            if not name:
                continue
            image_url = ""
            if img_el:
                image_url = img_el.get_attribute("src") or img_el.get_attribute("data-src") or ""
            images[_normalize(name)] = image_url
        except Exception:
            continue
    return images


def enrich(restaurants: list[dict], limit: int | None) -> tuple[int, int]:
    """Enriches wolt_menu dishes with image_url. Returns (venues_done, dishes_updated)."""
    targets = [r for r in restaurants if r.get("wolt_slug") and r.get("wolt_menu")]
    if limit:
        targets = targets[:limit]

    total_venues = len(targets)
    venues_done = 0
    dishes_updated = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        for idx, r in enumerate(targets, 1):
            slug = r["wolt_slug"]
            name = r.get("name", slug)
            print(f"[{idx}/{total_venues}] {name} ({slug})", end=" … ", flush=True)

            images = scrape_images(page, slug)
            if not images:
                print("no cards")
                time.sleep(1)
                continue

            updated = 0
            for dish in r["wolt_menu"]:
                norm = _normalize(dish.get("name", ""))
                url = images.get(norm, "")
                if url and not dish.get("image_url"):
                    dish["image_url"] = url
                    updated += 1

            dishes_updated += updated
            venues_done += 1
            print(f"{updated}/{len(r['wolt_menu'])} images")
            time.sleep(1.2)

        browser.close()

    return venues_done, dishes_updated


def atomic_save(data: list[dict]) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(DATA_FILE)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Обработать только первые N ресторанов (для теста)")
    args = parser.parse_args()

    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    print(f"Загружено {len(restaurants)} ресторанов")

    # Backup
    BACKUP_FILE.write_bytes(DATA_FILE.read_bytes())
    print(f"Бэкап: {BACKUP_FILE}")

    targets = [r for r in restaurants if r.get("wolt_slug") and r.get("wolt_menu")]
    print(f"Ресторанов с wolt_slug + wolt_menu: {len(targets)}"
          + (f" (лимит: {args.limit})" if args.limit else ""))

    venues_done, dishes_updated = enrich(restaurants, args.limit)

    atomic_save(restaurants)
    print(f"\nГотово. Ресторанов: {venues_done}, блюд с фото: {dishes_updated}")
    print(f"Сохранено в {DATA_FILE}")


if __name__ == "__main__":
    main()
