#!/usr/bin/env python3
"""
Enrich chain restaurant menu items from official nutrition PDFs/sources.

Currently supported:
  - dean&david: Official nutrition PDF (per-portion values)
    Source: https://deananddavid.com (November 2024 + February 2025 PDFs)

Sets confidence = "high", source = "official" on matched items.
Skips items already marked source in PROTECTED_SOURCES.

Usage:
  python3 enrichment/chains_enricher.py
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import pdfplumber
import requests
from fuzzywuzzy import fuzz

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup_chains.json"
PDF_DIR = ROOT / "enrichment" / "pdfs"
LOG_FILE = ROOT / "enrichment.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

PROTECTED_SOURCES = {"official", "subway_pdf", "manual"}
FUZZY_THRESHOLD = 60

HEADERS = {"User-Agent": "NutritionFinderBerlin/1.0 (educational project)"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_num(val) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip().replace(",", ".").replace("<", "").replace(" ", "")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def download_pdf(url: str, cache_path: Path) -> Optional[bytes]:
    if cache_path.exists():
        print(f"  Using cached {cache_path.name}")
        return cache_path.read_bytes()
    print(f"  Downloading {url} …")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        if not r.content.startswith(b"%PDF"):
            log.warning("URL did not return a PDF: %s", url)
            return None
        cache_path.write_bytes(r.content)
        return r.content
    except Exception as exc:
        log.error("Download failed %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Nordsee scraper (Playwright)
# ---------------------------------------------------------------------------

def scrape_nordsee_nutrition() -> dict[str, dict]:
    """Scrape per-100g nutrition from nordsee.com/de/produkte. Caches result."""
    if NORDSEE_CACHE.exists():
        print(f"  Using cached {NORDSEE_CACHE.name}")
        return json.loads(NORDSEE_CACHE.read_text())

    print("  Scraping nordsee.com with Playwright…")
    from playwright.sync_api import sync_playwright

    nutrition_db: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        cookies_accepted = False

        for cat in NORDSEE_CATEGORIES:
            url = f"{NORDSEE_SCRAPE_URL}/{cat}"
            try:
                page.goto(url, timeout=30000)
                page.wait_for_timeout(2000)
            except Exception as exc:
                log.warning("Nordsee: failed to load %s: %s", url, exc)
                continue

            if not cookies_accepted:
                try:
                    page.click('button:has-text("Cookies zulassen")', timeout=4000)
                    page.wait_for_timeout(2000)
                    cookies_accepted = True
                except Exception:
                    pass

            # Extract product names from page text
            text = page.inner_text("body")
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            products = []
            in_products = False
            for line in lines:
                if line in ("Highlights", "Snacks", "Tellergerichte",
                            "Vegetarisch / Vegan", "Desserts", "Getränke", "Salate", "Supermarkt"):
                    in_products = True
                    continue
                if line in ("Mehr laden", "SCHNAPP DIR DIE APP"):
                    break
                if in_products and len(line) > 3:
                    products.append(line)

            for product_name in products:
                if product_name.lower() in nutrition_db:
                    continue
                try:
                    page.click(f'text="{product_name}"', timeout=3000)
                    page.wait_for_timeout(1500)

                    body_lines = [l.strip() for l in page.inner_text("body").split("\n") if l.strip()]
                    vals: dict[str, float] = {}
                    for i, line in enumerate(body_lines):
                        next_val = body_lines[i + 1] if i + 1 < len(body_lines) else ""
                        try:
                            if "Brennwerte (kcal)" in line:
                                vals["calories"] = float(next_val.replace(",", "."))
                            elif "Eiweiß" in line:
                                vals["protein"] = float(next_val.replace(",", "."))
                            elif "Kohlenhydrate" in line:
                                vals["carbs"] = float(next_val.replace(",", "."))
                            elif line == "Fett (g)":
                                vals["fat"] = float(next_val.replace(",", "."))
                        except ValueError:
                            pass

                    if len(vals) == 4:
                        nutrition_db[product_name.lower()] = {
                            "name": product_name,
                            "calories_100g": vals["calories"],
                            "protein_100g":  vals["protein"],
                            "fat_100g":      vals["fat"],
                            "carbs_100g":    vals["carbs"],
                            # Main fields = per-100g (official Nordsee data is per 100g)
                            "calories": vals["calories"],
                            "protein":  vals["protein"],
                            "fat":      vals["fat"],
                            "carbs":    vals["carbs"],
                        }

                    try:
                        page.click('text="Zurück"', timeout=2000)
                    except Exception:
                        page.goto(url, timeout=15000)
                    page.wait_for_timeout(1000)

                except Exception as exc:
                    log.warning("Nordsee: error on %r: %s", product_name, exc)
                    page.goto(url, timeout=15000)
                    page.wait_for_timeout(1000)

        browser.close()

    NORDSEE_CACHE.write_text(json.dumps(nutrition_db, ensure_ascii=False, indent=2))
    print(f"  Scraped {len(nutrition_db)} products, cached to {NORDSEE_CACHE.name}")
    return nutrition_db


# ---------------------------------------------------------------------------
# dean&david parser
# ---------------------------------------------------------------------------

NORDSEE_SCRAPE_URL = "https://www.nordsee.com/de/produkte"
NORDSEE_CACHE = ROOT / "enrichment" / "nordsee_nutrition.json"

NORDSEE_CATEGORIES = [
    "highlights", "snacks", "tellergerichte",
    "vegetarisch_-_vegan", "desserts", "salate",
]

DEANANDDAVID_PDFS = [
    (
        "Nov 2024",
        "https://deananddavid.com/wp-content/uploads/2024/12/2024-11_dd_Nutritional-Values_until_28.02.2025.pdf",
        PDF_DIR / "deananddavid.pdf",
    ),
    (
        "Feb 2025",
        "https://deananddavid.com/wp-content/uploads/2025/03/2025-02_dd_Nahrwertubersicht_gultig-bis_31.05.2025.pdf",
        PDF_DIR / "deananddavid_feb2025.pdf",
    ),
]

# Column indices in the PDF table (0-based):
# [2]=name, [6]=kcal/100g, [7]=kcal/portion, [8]=fat/100g, [9]=fat/portion,
# [12]=carbs/100g, [13]=carbs/portion, [16]=protein/100g, [17]=protein/portion
_DD_COL_NAME       = 2
_DD_COL_KCAL_100   = 6
_DD_COL_KCAL_POR   = 7
_DD_COL_FAT_100    = 8
_DD_COL_FAT_POR    = 9
_DD_COL_CARB_100   = 12
_DD_COL_CARB_POR   = 13
_DD_COL_PROT_100   = 16
_DD_COL_PROT_POR   = 17

_DD_SKIP = {"Product", "Produkt", ""}


def _parse_deananddavid_pdf(content: bytes) -> dict[str, dict]:
    """Parse dean&david PDF, return {name_lower: nutrition}."""
    dishes: dict[str, dict] = {}
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if len(row) <= _DD_COL_PROT_POR:
                            continue
                        name = (row[_DD_COL_NAME] or "").strip()
                        if not name or name in _DD_SKIP:
                            continue
                        kcal_p  = parse_num(row[_DD_COL_KCAL_POR])
                        fat_p   = parse_num(row[_DD_COL_FAT_POR])
                        carb_p  = parse_num(row[_DD_COL_CARB_POR])
                        prot_p  = parse_num(row[_DD_COL_PROT_POR])
                        kcal_100 = parse_num(row[_DD_COL_KCAL_100])
                        fat_100  = parse_num(row[_DD_COL_FAT_100])
                        carb_100 = parse_num(row[_DD_COL_CARB_100])
                        prot_100 = parse_num(row[_DD_COL_PROT_100])
                        if kcal_p and fat_p and carb_p and prot_p:
                            dishes[name.lower()] = {
                                "name": name,
                                "calories":    round(kcal_p),
                                "fat":         round(fat_p, 1),
                                "carbs":       round(carb_p, 1),
                                "protein":     round(prot_p, 1),
                                "calories_100g": round(kcal_100, 1) if kcal_100 else None,
                                "fat_100g":      round(fat_100, 1)  if fat_100  else None,
                                "carbs_100g":    round(carb_100, 1) if carb_100 else None,
                                "protein_100g":  round(prot_100, 1) if prot_100 else None,
                            }
    except Exception as exc:
        log.error("dean&david PDF parse error: %s", exc)
    return dishes


def load_deananddavid_nutrition() -> dict[str, dict]:
    """Load and merge both dean&david PDFs (newer takes precedence)."""
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    merged: dict[str, dict] = {}
    for label, url, cache in DEANANDDAVID_PDFS:
        content = download_pdf(url, cache)
        if not content:
            continue
        dishes = _parse_deananddavid_pdf(content)
        print(f"  {label}: {len(dishes)} items parsed")
        merged.update(dishes)  # later PDFs overwrite earlier ones
    return merged


def enrich_chain(
    restaurants: list[dict],
    chain_name_substr: str,
    nutrition_db: dict[str, dict],
    source_label: str,
    threshold: int = FUZZY_THRESHOLD,
) -> tuple[int, int]:
    """
    For all restaurants matching chain_name_substr, fuzzy-match wolt_menu items
    against nutrition_db and update. Returns (matched, unmatched).
    """
    matched = unmatched = 0
    db_names = list(nutrition_db.keys())

    for restaurant in restaurants:
        if chain_name_substr.lower() not in restaurant.get("name", "").lower():
            continue

        for field in ("wolt_menu", "site_menu"):
            for item in restaurant.get(field, []):
                if item.get("source") in PROTECTED_SOURCES:
                    continue

                item_name = item.get("name", "").strip()
                if not item_name:
                    unmatched += 1
                    continue

                # Fuzzy match against all PDF entries
                best_score = 0
                best_key = None
                item_lower = item_name.lower()
                for db_key in db_names:
                    score = fuzz.token_set_ratio(item_lower, db_key)
                    if score > best_score:
                        best_score = score
                        best_key = db_key

                if best_score >= threshold and best_key:
                    data = nutrition_db[best_key]
                    item["calories"] = data["calories"]
                    item["protein"]  = data["protein"]
                    item["fat"]      = data["fat"]
                    item["carbs"]    = data["carbs"]
                    if data.get("calories_100g"):
                        item["calories_100g"] = data["calories_100g"]
                        item["protein_100g"]  = data["protein_100g"]
                        item["fat_100g"]      = data["fat_100g"]
                        item["carbs_100g"]    = data["carbs_100g"]
                    item["confidence"] = "high"
                    item["source"]     = source_label
                    log.info("MATCH [%s] %r → %r (score=%d)", chain_name_substr, item_name, data["name"], best_score)
                    matched += 1
                else:
                    log.info("NO MATCH [%s] %r (best=%d %r)", chain_name_substr, item_name, best_score, best_key)
                    unmatched += 1

    return matched, unmatched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE}\n")

    total_matched = total_unmatched = 0

    # ── Nordsee ─────────────────────────────────────────────────────────────
    print("=== Nordsee ===")
    nordsee_nutrition = scrape_nordsee_nutrition()
    print(f"  Total items in DB: {len(nordsee_nutrition)}")
    m, u = enrich_chain(restaurants, "nordsee", nordsee_nutrition, "official", threshold=55)
    print(f"  Matched: {m}  Unmatched: {u}")
    total_matched += m
    total_unmatched += u

    # ── dean&david ──────────────────────────────────────────────────────────
    print("=== dean&david ===")
    dd_nutrition = load_deananddavid_nutrition()
    print(f"  Total items in DB: {len(dd_nutrition)}")
    m, u = enrich_chain(restaurants, "dean&david", dd_nutrition, "official")
    print(f"  Matched: {m}  Unmatched: {u}")
    total_matched += m
    total_unmatched += u

    # ── Save ────────────────────────────────────────────────────────────────
    DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"\nSaved → {DATA_FILE}")
    print(f"Total matched: {total_matched}  Total unmatched: {total_unmatched}")
    log.info("chains_enricher complete. matched=%d unmatched=%d", total_matched, total_unmatched)


if __name__ == "__main__":
    main()
