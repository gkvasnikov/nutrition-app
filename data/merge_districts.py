"""
Объединяет рестораны всех районов в data/all_restaurants.json.

Районы:
  data/restaurants.json                          → mitte
  data/kreuzberg/restaurants_kreuzberg.json      → kreuzberg
  data/wrangelkiez/restaurants_wrangelkiez.json  → wrangelkiez (если есть)

Дедупликация по google_place_id / place_id.

Использование:
  python3 data/merge_districts.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
MITTE_FILE       = ROOT / "data" / "restaurants.json"
KREUZBERG_FILE   = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
WRANGELKIEZ_FILE = ROOT / "data" / "wrangelkiez" / "restaurants_wrangelkiez.json"
OUT_FILE         = ROOT / "data" / "all_restaurants.json"


def canonical_id(r: dict) -> str | None:
    return r.get("google_place_id") or r.get("place_id") or r.get("id")


def normalise(r: dict, district: str) -> dict:
    out = dict(r)
    out["district"] = district
    if "id" in out and "google_place_id" not in out:
        out["google_place_id"] = out["id"]
    if "place_id" in out and "google_place_id" not in out:
        out["google_place_id"] = out["place_id"]
    return out


def run() -> None:
    sources = [
        (MITTE_FILE, "mitte"),
        (KREUZBERG_FILE, "kreuzberg"),
        (WRANGELKIEZ_FILE, "wrangelkiez"),
    ]

    seen: set[str] = set()
    merged: list[dict] = []
    counts: dict[str, int] = {}
    dups = 0

    for fpath, district in sources:
        if not fpath.exists():
            print(f"{district:<12}: файл не найден, пропускаем")
            continue
        data = json.loads(fpath.read_text())
        counts[district] = len(data)
        added = 0
        for r in data:
            r = normalise(r, district)
            cid = canonical_id(r)
            if cid and cid in seen:
                dups += 1
                continue
            if cid:
                seen.add(cid)
            merged.append(r)
            added += 1
        print(f"{district:<12}: {len(data)} ресторанов  (+{added} новых)")

    OUT_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2))

    print(f"Дубликатов  : {dups}")
    print(f"Итого       : {len(merged)} ресторанов")
    print(f"Файл        : {OUT_FILE}")


if __name__ == "__main__":
    run()
