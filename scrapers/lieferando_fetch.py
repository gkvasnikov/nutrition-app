#!/usr/bin/env python3
"""
Scrape Lieferando menus for Berlin Mitte restaurants via Playwright + stealth.

Steps:
  1. Open lieferando.de/en/delivery/food/10178, scroll to collect all restaurant slugs
  2. For each slug scrape the menu page (scroll to load all categories)
  3. Fuzzy-match each Lieferando venue to restaurants.json by name
  4. If Lieferando item count > existing menu item count → replace menu data
     and set menu_source = "lieferando", otherwise keep existing data
  5. Save updated restaurants.json (with backup)
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from fuzzywuzzy import fuzz
from playwright.sync_api import sync_playwright, Page
from playwright_stealth import Stealth

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup.json"

logging.basicConfig(
    filename=ROOT / "errors.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

LIEFERANDO_LIST = "https://www.lieferando.de/en/delivery/food/10178"
LIEFERANDO_MENU = "https://www.lieferando.de{slug}"

MATCH_THRESHOLD = 65


# ---------------------------------------------------------------------------
# Step 1 — collect restaurant slugs from listing page
# ---------------------------------------------------------------------------

def collect_slugs(page: Page) -> list[dict]:
    """Return list of {slug, name} dicts from the Berlin Mitte listing."""
    print("Loading Lieferando listing…")
    page.goto(LIEFERANDO_LIST, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(2000)

    seen_slugs: set[str] = set()
    prev_count = 0

    for _ in range(20):
        page.keyboard.press("End")
        page.wait_for_timeout(1000)
        count = len(page.query_selector_all("a[href*='/menu/']"))
        if count == prev_count:
            break
        prev_count = count

    entries = page.eval_on_selector_all(
        "a[href*='/menu/']",
        """els => els.map(e => {
            const href = e.getAttribute('href').split('?')[0];
            const slug = href.replace('/en/menu/', '');
            const name = e.querySelector('h2,h3,[class*=name],[class*=title]')?.innerText?.trim()
                      || e.innerText.split('\\n')[0].trim();
            return {slug, href, name};
        })""",
    )

    results = []
    for e in entries:
        slug = e["slug"]
        if not slug or slug in seen_slugs:
            continue
        # Skip non-food entries (pharmacies, supermarkets)
        if any(x in slug for x in ["apotheke", "rewe", "dm-drogerie", "aldi", "lidl"]):
            continue
        seen_slugs.add(slug)
        results.append({"slug": e["href"], "name": e["name"]})

    print(f"  {len(results)} restaurant slugs collected")
    return results


# ---------------------------------------------------------------------------
# Step 2 — scrape menu from a single restaurant page
# ---------------------------------------------------------------------------

def _parse_price(text: str) -> Optional[float]:
    m = re.search(r"(\d+)[,\.](\d+)", text)
    return float(f"{m.group(1)}.{m.group(2)}") if m else None


def _extract_weight(text: str) -> str:
    m = re.search(r"\b(\d+)\s*(g|ml|cl|l)\b", text, re.I)
    return f"{m.group(1)}{m.group(2).lower()}" if m else ""


def scrape_menu(page: Page, slug_path: str, restaurant_name: str) -> list[dict]:
    url = LIEFERANDO_MENU.format(slug=slug_path)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        page.wait_for_timeout(1500)
    except Exception as exc:
        logging.error("Timeout loading %s: %s", url, exc)
        return []

    # Scroll to trigger lazy-loaded categories
    prev_count = 0
    for _ in range(15):
        page.keyboard.press("End")
        page.wait_for_timeout(600)
        count = len(page.query_selector_all("[data-item-id]"))
        if count == prev_count:
            break
        prev_count = count

    cards = page.query_selector_all("[data-item-id]")
    items = []
    for card in cards:
        try:
            text_lines = [ln.strip() for ln in card.inner_text().split("\n") if ln.strip()]
            # Filter out "Item Info" label
            text_lines = [ln for ln in text_lines if ln.lower() not in ("item info",)]

            name = text_lines[0] if text_lines else ""
            if not name:
                continue

            # Price is the line matching "X,XX €"
            price_raw = next((ln for ln in text_lines if re.search(r"\d+[,\.]\d+\s*€", ln)), "")
            price = _parse_price(price_raw)

            # Description: remaining lines that are not price/unit-price
            desc_lines = [
                ln for ln in text_lines[1:]
                if ln != price_raw
                and not re.match(r"^\d+[,\.]\d+\s*€/", ln)  # skip unit price "21,00 €/1l"
            ]
            description = " ".join(desc_lines)

            weight = _extract_weight(name + " " + description)
            # Also check for volume in price block (e.g. "0.3l")
            vol = next(
                (ln for ln in text_lines if re.match(r"^\d+[\.,]\d+\s*[lL]$", ln.strip())),
                ""
            )
            if not weight and vol:
                m = re.match(r"(\d+[\.,]\d+)\s*([lL])", vol)
                if m:
                    weight = f"{int(float(m.group(1).replace(',', '.')) * 1000)}ml"

            items.append({
                "name": name,
                "description": description,
                "price": price,
                "weight": weight,
            })
        except Exception as exc:
            logging.error("Card parse error for %s: %s", slug_path, exc)

    return items


# ---------------------------------------------------------------------------
# Step 3 — fuzzy match
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def find_best_match(lief_name: str, restaurants: list[dict]) -> Optional[int]:
    best_idx, best_score = None, 0
    norm = _normalize(lief_name)
    for i, r in enumerate(restaurants):
        score = fuzz.token_set_ratio(norm, _normalize(r.get("name", "")))
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx if best_score >= MATCH_THRESHOLD else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    print(f"Loaded {len(restaurants)} restaurants from {DATA_FILE}")

    BACKUP_FILE.write_bytes(DATA_FILE.read_bytes())
    print(f"Backup saved to {BACKUP_FILE}")

    replaced = 0
    kept_wolt = 0
    unmatched = 0
    no_menu = 0

    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        venues = collect_slugs(page)

        for idx, venue in enumerate(venues, 1):
            name = venue["name"]
            slug = venue["slug"]
            print(f"[{idx}/{len(venues)}] {name}")

            menu = scrape_menu(page, slug, name)
            if not menu:
                print(f"  — no items")
                no_menu += 1
                time.sleep(1)
                continue

            print(f"  scraped {len(menu)} items")

            match_idx = find_best_match(name, restaurants)
            if match_idx is None:
                print(f"  — no OSM match")
                unmatched += 1
                time.sleep(1)
                continue

            osm = restaurants[match_idx]
            existing_menu = osm.get("wolt_menu", [])
            existing_count = len(existing_menu)
            existing_source = osm.get("menu_source", "wolt" if existing_menu else "none")

            if len(menu) > existing_count:
                osm["wolt_menu"] = menu
                osm["menu_source"] = "lieferando"
                print(f"  ✓ replaced '{osm['name']}' ({existing_count} {existing_source} → {len(menu)} lieferando)")
                replaced += 1
            else:
                print(f"  = kept '{osm['name']}' ({existing_count} {existing_source} ≥ {len(menu)} lieferando)")
                kept_wolt += 1

            time.sleep(1.5)

        browser.close()

    DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))

    print(f"\nDone.")
    print(f"  Replaced with Lieferando: {replaced}")
    print(f"  Kept existing (Wolt/OSM): {kept_wolt}")
    print(f"  No OSM match:             {unmatched}")
    print(f"  No menu scraped:          {no_menu}")
    print(f"Saved to {DATA_FILE}")


if __name__ == "__main__":
    main()
