"""
Добавляет поле weight_grams_estimate ко всем блюдам, у которых его нет.
Использует Claude Batches API (claude-haiku-4-5) — только граммовка, не КБЖУ.

Запуск:
  python3 enrichment/weight_estimator.py
  python3 enrichment/weight_estimator.py --file data/kreuzberg/restaurants_kreuzberg.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)

DATA_FILE   = ROOT / "data" / "all_restaurants.json"
MODEL       = "claude-haiku-4-5"
MAX_TOKENS  = 60
MENU_FIELDS = ["wolt_menu", "site_menu"]

logging.basicConfig(filename=ROOT / "errors.log", level=logging.ERROR,
                    format="%(asctime)s %(levelname)s %(message)s")

SYSTEM = (
    "You are a restaurant portion expert. "
    "Given a dish name, description and cuisine, estimate the typical portion weight in grams "
    "as served in a mid-range Berlin restaurant. "
    "Return ONLY valid JSON: {\"weight_grams\": <integer>}"
)


def _prompt(item: dict, cuisine: str) -> str:
    parts = [f"Dish: {item['name']}"]
    if item.get("description"):
        parts.append(f"Description: {item['description']}")
    if cuisine:
        parts.append(f"Cuisine: {cuisine}")
    parts.append("Estimate portion weight in grams. Return JSON only.")
    return "\n".join(parts)


def _custom_id(r_idx: int, i_idx: int, field: str) -> str:
    f = "w" if field == "wolt_menu" else "s"
    return f"r{r_idx}-i{i_idx}-{f}"


def _parse_id(cid: str) -> tuple[int, int, str]:
    m = re.match(r"r(\d+)-i(\d+)-([ws])", cid)
    if not m:
        raise ValueError(cid)
    field = "wolt_menu" if m.group(3) == "w" else "site_menu"
    return int(m.group(1)), int(m.group(2)), field


def collect_requests(restaurants: list[dict]) -> list[Request]:
    reqs = []
    for r_idx, r in enumerate(restaurants):
        cuisine = r.get("cuisine", "")
        for field in MENU_FIELDS:
            for i_idx, item in enumerate(r.get(field, [])):
                if item.get("weight_grams_estimate"):
                    continue
                if not item.get("calories"):
                    continue
                reqs.append(Request(
                    custom_id=_custom_id(r_idx, i_idx, field),
                    params=MessageCreateParamsNonStreaming(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM,
                        messages=[{"role": "user", "content": _prompt(item, cuisine)}],
                    ),
                ))
    return reqs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None)
    args = parser.parse_args()

    data_file = Path(args.file) if args.file else DATA_FILE
    batch_id_file = ROOT / "enrichment" / f"weight_{data_file.stem}_batch_id.txt"

    restaurants = json.loads(data_file.read_text())
    total = sum(len(r.get(f, [])) for r in restaurants for f in MENU_FIELDS)
    print(f"Загружено: {len(restaurants)} ресторанов, {total} блюд")

    # Backup
    backup = data_file.with_name(data_file.stem + "_backup_weight.json")
    backup.write_bytes(data_file.read_bytes())
    print(f"Бэкап: {backup.name}")

    client = anthropic.Anthropic()

    if batch_id_file.exists():
        batch_id = batch_id_file.read_text().strip()
        print(f"Resuming batch: {batch_id}")
    else:
        reqs = collect_requests(restaurants)
        print(f"Блюд без граммовки: {len(reqs)}")
        if not reqs:
            print("Все блюда уже имеют граммовку.")
            return
        print(f"Отправляю батч из {len(reqs)} запросов…")
        batch = client.messages.batches.create(requests=reqs)
        batch_id = batch.id
        batch_id_file.write_text(batch_id)
        print(f"Batch ID: {batch_id}")

    # Poll
    while True:
        b = client.messages.batches.retrieve(batch_id)
        c = b.request_counts
        print(f"  {b.processing_status} processing={c.processing} succeeded={c.succeeded} errored={c.errored}")
        if b.processing_status == "ended":
            break
        time.sleep(30)

    # Apply
    applied = errors = 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            errors += 1
            continue
        text = next((b.text for b in result.result.message.content if b.type == "text"), "")
        try:
            clean = re.sub(r"^```[a-z]*\n?|\n?```$", "", text.strip())
            data = json.loads(clean)
            w = data.get("weight_grams")
            if not w or not isinstance(w, (int, float)) or w <= 0:
                continue
            r_idx, i_idx, field = _parse_id(result.custom_id)
            restaurants[r_idx][field][i_idx]["weight_grams_estimate"] = int(w)
            applied += 1
        except Exception as e:
            logging.error("weight parse error %s: %s | %s", result.custom_id, e, text)
            errors += 1

    data_file.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    batch_id_file.unlink(missing_ok=True)
    print(f"\nГотово. Добавлено граммовок: {applied}. Ошибок: {errors}.")
    print(f"Сохранено в {data_file}")


if __name__ == "__main__":
    main()
