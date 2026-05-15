#!/usr/bin/env python3
"""
Parse Subway Germany nutrition PDF and update КБЖУ in restaurants.json
for all Subway locations, matching wolt_menu items to PDF dishes via fuzzy matching.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

import pdfplumber
from fuzzywuzzy import fuzz

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup_subway.json"
PDF_FILE = Path("/Users/george/Downloads/German Nutritional Information Full Menu March 2025_de-de.pdf")

FUZZY_THRESHOLD = 65

SKIP_NAMES = {
    "Sandwiches (15 cm)", "Sandwiches (30 cm)", "Wraps", "Salads",
    "Panini", "Unsere Kreationen Subs (15 cm)", "Unsere Kreationen Subs (30 cm)",
    "Kids Meals", "Desserts", "Getränke", "Drinks",
    "Zutaten", "Toppings", "Saucen", "Sauces",
    "Name", "Bezeichnung", "",
}


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


def parse_pdf(pdf_path: Path) -> dict[str, dict]:
    """Parse PDF, return dict keyed by dish name (lowercase) → nutrition data."""
    dishes: dict[str, dict] = {}
    current_category = None

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            table = tables[0]
            for row in table:
                if not row or not row[0]:
                    continue
                name = str(row[0]).strip()

                # Skip header/category rows
                if name in SKIP_NAMES or name.startswith("Nährwert") or name.startswith("Portion"):
                    if name in SKIP_NAMES and name not in {"", "Name", "Bezeichnung"}:
                        current_category = name
                    continue

                kcal = parse_num(row[3] if len(row) > 3 else None)
                fat = parse_num(row[4] if len(row) > 4 else None)
                carbs = parse_num(row[6] if len(row) > 6 else None)
                protein = parse_num(row[9] if len(row) > 9 else None)
                weight = parse_num(row[1] if len(row) > 1 else None)

                if kcal is None or kcal < 10:
                    continue

                key = name.lower()
                dishes[key] = {
                    "original_name": name,
                    "category": current_category,
                    "calories": round(kcal),
                    "fat": fat,
                    "carbs": carbs,
                    "protein": protein,
                    "weight": f"{round(weight)}g" if weight else None,
                    "confidence": "high",
                }

    return dishes


def best_match(menu_name: str, pdf_dishes: dict[str, dict]) -> Optional[tuple[str, int]]:
    """Find best fuzzy match for a menu item name among PDF dish names."""
    name_lower = menu_name.lower()
    best_key = None
    best_score = 0

    for key in pdf_dishes:
        score = fuzz.token_set_ratio(name_lower, key)
        if score > best_score:
            best_score = score
            best_key = key

    if best_score >= FUZZY_THRESHOLD:
        return best_key, best_score
    return None


def main() -> None:
    print(f"Loading restaurants from {DATA_FILE}")
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())

    # Backup
    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup saved to {BACKUP_FILE}")

    # Parse PDF
    print(f"Parsing PDF: {PDF_FILE}")
    pdf_dishes = parse_pdf(PDF_FILE)
    print(f"  Parsed {len(pdf_dishes)} dishes from PDF")

    # Find Subway restaurants
    subway_restaurants = [
        (i, r) for i, r in enumerate(restaurants)
        if "subway" in r.get("name", "").lower()
    ]
    print(f"Found {len(subway_restaurants)} Subway restaurants")

    total_updated = 0
    total_checked = 0
    unmatched: list[str] = []

    for r_idx, restaurant in subway_restaurants:
        menu = restaurant.get("wolt_menu", [])
        if not menu:
            print(f"  {restaurant['name']} — no wolt_menu, skipping")
            continue

        updated = 0
        for item in menu:
            total_checked += 1
            match = best_match(item["name"], pdf_dishes)
            if match:
                key, score = match
                data = pdf_dishes[key]
                item["calories"] = data["calories"]
                item["protein"] = data["protein"]
                item["fat"] = data["fat"]
                item["carbs"] = data["carbs"]
                item["confidence"] = "high"
                item["source"] = "subway_pdf"
                if data["weight"]:
                    item["weight"] = data["weight"]
                updated += 1
                total_updated += 1
            else:
                unmatched.append(f"{restaurant['name']}: {item['name']}")

        print(f"  {restaurant['name']} — updated {updated}/{len(menu)} items")

    print(f"\nTotal: {total_updated}/{total_checked} items updated across {len(subway_restaurants)} Subway locations")

    if unmatched:
        print(f"\nUnmatched items ({len(unmatched)}):")
        seen = set()
        for s in unmatched:
            dish = s.split(": ", 1)[1]
            if dish not in seen:
                seen.add(dish)
                print(f"  - {dish}")

    DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"\nSaved to {DATA_FILE}")


if __name__ == "__main__":
    main()
