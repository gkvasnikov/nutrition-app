"""
Импортирует меню из data/kreuzberg/menu_texts/*.txt в restaurants_kreuzberg.json.
Добавляет только рестораны без существующего меню.
Матчинг по имени: порог ≥ 85 (консервативный).

Использование:
  python3 scrapers/kreuzberg/menu_text_importer.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fuzzywuzzy import fuzz

ROOT = Path(__file__).parent.parent.parent
RESTAURANTS_FILE = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
MENU_DIR         = ROOT / "data" / "kreuzberg" / "menu_texts"

MATCH_THRESHOLD = 85


def normalize(s: str) -> str:
    return re.sub(r"[^\w\s]", "", s.lower()).strip()


def parse_menu_file(path: Path) -> tuple[str, list[dict]]:
    """Возвращает (название ресторана из файла, список блюд)."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # Первая строка # — название ресторана
    rest_name = ""
    for line in lines:
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            rest_name = line.lstrip("# ").strip()
            break

    # Проверяем что файл не image-only
    if "IMAGE-BASED" in text or "OCR erforderlich" in text:
        return rest_name, []

    items = []
    current_category = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Категории
        if line.startswith("###") or line.startswith("####"):
            current_category = re.sub(r"^#+\s*", "", line).strip()
            continue

        # Блюда: содержат " | "
        if " | " in line:
            parts = [p.strip() for p in line.split(" | ")]
            name = parts[0]
            if not name or len(name) < 2:
                continue

            # Описание: нет (первая часть — это имя+описание вместе)
            # Цена: ищем EUR
            price = None
            for p in parts[1:]:
                m = re.search(r"([\d]+[,.][\d]+)", p)
                if m and "EUR" in p:
                    try:
                        price = float(m.group(1).replace(",", "."))
                    except Exception:
                        pass
                    break

            items.append({
                "name": name,
                "category": current_category,
                "price": price,
            })
            continue

        # Блюда без | (просто строка без цены) — пропускаем, слишком много шума

    return rest_name, items


def find_match(file_name: str, no_menu_rests: list[dict]) -> tuple[dict | None, int]:
    best_r, best_score = None, 0
    fn = normalize(file_name)
    for r in no_menu_rests:
        score = fuzz.token_set_ratio(fn, normalize(r["name"]))
        if score > best_score:
            best_score, best_r = score, r
    if best_score >= MATCH_THRESHOLD:
        return best_r, best_score
    return None, best_score


def run() -> None:
    restaurants = json.loads(RESTAURANTS_FILE.read_text())

    # Только рестораны без какого-либо меню
    no_menu = [
        r for r in restaurants
        if not r.get("wolt_menu")
        and not r.get("ubereats_menu")
        and not r.get("menu")
        and not r.get("site_menu")
    ]
    print(f"Ресторанов без меню: {len(no_menu)}")

    files = sorted(MENU_DIR.glob("*.txt"))
    print(f"Файлов меню: {len(files)}")

    matched = 0
    skipped_image = 0
    skipped_no_match = 0
    total_dishes = 0

    for f in files:
        # Имя из filename
        fname = re.sub(r"^\d+_", "", f.stem).replace("_", " ")

        rest_name, items = parse_menu_file(f)

        if not items:
            skipped_image += 1
            name_display = rest_name or fname
            print(f"  [IMAGE] {name_display}")
            continue

        match, score = find_match(fname, no_menu)
        if not match:
            print(f"  [NO MATCH {score}] {fname}")
            skipped_no_match += 1
            continue

        print(f"  [✓ {score}] {fname[:35]:35} → {match['name'][:35]} ({len(items)} блюд)")
        match["menu_text"] = items
        match["menu_text_source"] = f.name
        matched += 1
        total_dishes += len(items)

    RESTAURANTS_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))

    print(f"\n{'─'*50}")
    print(f"Добавлено ресторанов : {matched}")
    print(f"Блюд из текстов      : {total_dishes}")
    print(f"Image-only (пропуск) : {skipped_image}")
    print(f"Не найдено в БД      : {skipped_no_match}")


if __name__ == "__main__":
    run()
