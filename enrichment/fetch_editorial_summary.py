"""
Добавляет поле editorial_summary (описание от Google) для ресторанов с меню.

Запуск:
  python3 enrichment/fetch_editorial_summary.py
  python3 enrichment/fetch_editorial_summary.py --file data/all_restaurants.json
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

DATA_FILE = ROOT / "data" / "all_restaurants.json"
PAUSE_S = 0.05  # 50ms между запросами — хватает для бесплатного лимита

DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
MENU_FIELDS = ["wolt_menu", "site_menu"]


def has_menu(r: dict) -> bool:
    return any(r.get(f) for f in MENU_FIELDS)


def fetch_summary(place_id: str, api_key: str) -> str | None:
    resp = requests.get(
        DETAILS_URL,
        params={
            "place_id": place_id,
            "fields": "editorial_summary",
            "language": "ru",
            "key": api_key,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    result = data.get("result", {})
    summary = result.get("editorial_summary", {})
    return summary.get("overview") or None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None)
    args = parser.parse_args()

    data_file = Path(args.file) if args.file else DATA_FILE
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_PLACES_API_KEY не найден в .env")

    restaurants = json.loads(data_file.read_text())

    targets = [r for r in restaurants if has_menu(r) and r.get("google_place_id")]
    todo = [r for r in targets if not r.get("editorial_summary")]
    print(f"Ресторанов с меню: {len(targets)}")
    print(f"Без описания (нужно запросить): {len(todo)}")

    if not todo:
        print("Все уже имеют описание.")
        return

    # Бэкап
    backup = data_file.with_name(data_file.stem + "_backup_summary.json")
    if not backup.exists():
        backup.write_bytes(data_file.read_bytes())
        print(f"Бэкап: {backup.name}")

    found = skipped = errors = 0

    for i, r in enumerate(todo, 1):
        place_id = r["google_place_id"]
        try:
            summary = fetch_summary(place_id, api_key)
            if summary:
                r["editorial_summary"] = summary
                found += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  Ошибка {r['name']}: {e}")
            errors += 1

        if i % 50 == 0:
            print(f"  {i}/{len(todo)} — найдено: {found}, без описания: {skipped}, ошибок: {errors}")
            data_file.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))

        time.sleep(PAUSE_S)

    data_file.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"\nГотово. Найдено описаний: {found}. Без описания: {skipped}. Ошибок: {errors}.")
    print(f"Сохранено в {data_file}")


if __name__ == "__main__":
    main()
