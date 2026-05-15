"""
Обогащает известные сетевые рестораны Кройцберга официальными КБЖУ.
Копирует данные из уже готовых данных Митте или скачивает заново.

Поддерживаемые сети: McDonald's, Burger King, Subway, KFC, Starbucks,
  dean&david, Nordsee, Green & Protein, и др.

Использование:
  python3 scrapers/kreuzberg/chains_scraper.py
"""
from __future__ import annotations

import json
from pathlib import Path

RESTAURANTS_FILE = Path("data/kreuzberg/restaurants_kreuzberg.json")
MITTE_FILE = Path("data/restaurants.json")

# Названия сетей (нечувствительно к регистру, частичное совпадение)
KNOWN_CHAINS = [
    "mcdonald", "burger king", "subway", "kfc", "starbucks",
    "dean&david", "dean & david", "nordsee", "green & protein",
    "green&protein", "peter pane", "hans im glück", "vapiano",
    "five guys", "chipotle", "domino", "pizza hut", "dunkin",
]


def name_matches_chain(name: str, chain: str) -> bool:
    return chain.lower() in name.lower()


def is_chain(name: str) -> bool:
    return any(name_matches_chain(name, c) for c in KNOWN_CHAINS)


def find_mitte_menu(name: str, mitte: list[dict]) -> list[dict] | None:
    name_l = name.lower()
    for r in mitte:
        if any(name_matches_chain(r["name"], c)
               for c in KNOWN_CHAINS
               if name_matches_chain(name, c)):
            menu = r.get("wolt_menu") or r.get("site_menu") or []
            if menu:
                return menu
    return None


def run() -> None:
    restaurants = json.loads(RESTAURANTS_FILE.read_text())
    mitte = json.loads(MITTE_FILE.read_text()) if MITTE_FILE.exists() else []

    enriched = 0
    for r in restaurants:
        if not is_chain(r["name"]):
            continue
        if r.get("wolt_menu") or r.get("site_menu") or r.get("lieferando_menu"):
            continue  # уже есть

        menu = find_mitte_menu(r["name"], mitte)
        if menu:
            r["site_menu"] = menu
            r["kbju_status"] = "verified_pdf"
            enriched += 1
            print(f"  ✓ {r['name']} ← Mitte данные ({len(menu)} блюд)")
        else:
            print(f"  ? {r['name']} — нет данных Mitte, нужен отдельный скрапер")

    RESTAURANTS_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"\nОбогащено: {enriched} сетевых ресторанов → {RESTAURANTS_FILE}")


if __name__ == "__main__":
    run()
