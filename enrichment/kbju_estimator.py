#!/usr/bin/env python3
"""
Estimate calories/protein/fat/carbs for every menu item in restaurants.json
using the Claude Batches API (50% cost reduction, async processing).

Modes:
  Default  — estimate items that have no calories yet.
  --reimprove — re-estimate items where source='claude' and
                confidence in ('low', 'medium'), using a richer prompt.
                Skips items enriched from official/openfoodfacts/fatsecret sources.

Run once to submit + wait + save. If interrupted, rerun to resume using
the saved batch_id (skips resubmission, jumps straight to polling/collection).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import os
import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env", override=True)
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup.json"
BATCH_ID_FILE = ROOT / "enrichment" / "kbju_batch_id.txt"
REIMPROVE_BATCH_ID_FILE = ROOT / "enrichment" / "kbju_reimprove_batch_id.txt"

logging.basicConfig(
    filename=ROOT / "errors.log",
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 200
MAX_TOKENS_V2 = 300  # a bit more for reasoning field

# Sources that must never be overwritten
PROTECTED_SOURCES = {"official", "fatsecret"}

# ── Original prompt (first-pass estimation) ─────────────────────────────────
SYSTEM_PROMPT = (
    "You are a nutrition expert. Given a dish name, description, cuisine type, "
    "and optional weight/portion, estimate the nutritional content per serving. "
    "Always respond with valid JSON only — no markdown, no explanation. "
    'Format: {"calories": <int>, "protein": <float>, "fat": <float>, "carbs": <float>, '
    '"confidence": "<high|medium|low>"} '
    "confidence = high if weight/portion is given, medium if composition is clear, "
    "low if only the name is available."
)


def _build_prompt(item: dict, cuisine: str) -> str:
    parts = [f"Dish: {item['name']}"]
    if item.get("description"):
        parts.append(f"Description: {item['description']}")
    if cuisine:
        parts.append(f"Cuisine: {cuisine}")
    if item.get("weight"):
        parts.append(f"Portion/weight: {item['weight']}")
    parts.append("Estimate nutritional content per serving. Return JSON only.")
    return "\n".join(parts)


# ── Improved prompt (reimprove pass) ────────────────────────────────────────
SYSTEM_PROMPT_V2 = (
    "Ты опытный диетолог. Оцени КБЖУ блюда для типичной порции "
    "в берлинском ресторане среднего ценового сегмента. "
    "Отвечай ТОЛЬКО валидным JSON без пояснений и markdown."
)


def _build_prompt_v2(item: dict, cuisine: str) -> str:
    name = item.get("name", "")
    description = item.get("description") or ""
    price = item.get("price")
    weight = item.get("weight") or item.get("weight_grams_estimate")

    lines = [
        f"Название: {name}",
    ]
    if description:
        lines.append(f"Описание: {description}")
    if cuisine:
        lines.append(f"Кухня ресторана: {cuisine}")
    if price is not None:
        lines.append(f"Цена: {price} EUR (используй как косвенный индикатор размера порции)")
    if weight:
        lines.append(f"Известная граммовка: {weight}")

    lines += [
        "",
        "Учти:",
        "- Порции в берлинских ресторанах обычно 300-450г для горячего",
        "- Цена выше €15 обычно означает бо́льшую порцию",
        "- Для супов типичная порция 250-300мл",
        "",
        'Верни ТОЛЬКО JSON без пояснений:',
        '{',
        '  "calories": число,',
        '  "protein": число,',
        '  "fat": число,',
        '  "carbs": число,',
        '  "weight_grams_estimate": число,',
        '  "confidence": "medium" или "low",',
        '  "reasoning": "одна строка объяснения"',
        '}',
    ]
    return "\n".join(lines)


# ── Custom ID helpers ────────────────────────────────────────────────────────
MENU_FIELDS = ["wolt_menu", "site_menu"]


def _custom_id(rest_idx: int, item_idx: int, field: str) -> str:
    f = "w" if field == "wolt_menu" else "s"
    return f"r{rest_idx}-i{item_idx}-{f}"


def _parse_custom_id(custom_id: str) -> tuple[int, int, str]:
    m = re.match(r"r(\d+)-i(\d+)-([ws])", custom_id)
    if not m:
        m2 = re.match(r"r(\d+)-i(\d+)", custom_id)
        if not m2:
            raise ValueError(f"Unexpected custom_id: {custom_id}")
        return int(m2.group(1)), int(m2.group(2)), "wolt_menu"
    field = "wolt_menu" if m.group(3) == "w" else "site_menu"
    return int(m.group(1)), int(m.group(2)), field


# ── Item filters ─────────────────────────────────────────────────────────────
def _already_estimated(item: dict) -> bool:
    return "calories" in item and item["calories"] is not None


def _needs_reimprovement(item: dict) -> bool:
    """True if item was estimated by Claude and still has low/medium confidence.

    Covers two cases:
    - source='claude'  — explicitly tagged by a recent estimator run
    - source missing   — first-pass estimator didn't store source; these are
                         also Claude estimates and eligible for improvement
    """
    source = item.get("source", "")
    confidence = item.get("confidence", "")
    if source in PROTECTED_SOURCES:
        return False
    # Only items originally from Claude (explicit tag or missing tag = old first pass)
    if source not in ("claude", ""):
        return False
    return confidence in ("low", "medium")


# ── Request builders ─────────────────────────────────────────────────────────
def collect_requests(restaurants: list[dict]) -> list[Request]:
    """Collect items that have no calorie estimate yet (original mode)."""
    requests = []
    for r_idx, restaurant in enumerate(restaurants):
        cuisine = restaurant.get("cuisine", "")
        for field in MENU_FIELDS:
            for i_idx, item in enumerate(restaurant.get(field, [])):
                if _already_estimated(item):
                    continue
                prompt = _build_prompt(item, cuisine)
                requests.append(
                    Request(
                        custom_id=_custom_id(r_idx, i_idx, field),
                        params=MessageCreateParamsNonStreaming(
                            model=MODEL,
                            max_tokens=MAX_TOKENS,
                            system=SYSTEM_PROMPT,
                            messages=[{"role": "user", "content": prompt}],
                        ),
                    )
                )
    return requests


def collect_reimprove_requests(restaurants: list[dict]) -> list[Request]:
    """Collect source='claude' items with low/medium confidence for re-estimation."""
    requests = []
    for r_idx, restaurant in enumerate(restaurants):
        cuisine = restaurant.get("cuisine", "")
        for field in MENU_FIELDS:
            for i_idx, item in enumerate(restaurant.get(field, [])):
                if not _needs_reimprovement(item):
                    continue
                prompt = _build_prompt_v2(item, cuisine)
                requests.append(
                    Request(
                        custom_id=_custom_id(r_idx, i_idx, field),
                        params=MessageCreateParamsNonStreaming(
                            model=MODEL,
                            max_tokens=MAX_TOKENS_V2,
                            system=SYSTEM_PROMPT_V2,
                            messages=[{"role": "user", "content": prompt}],
                        ),
                    )
                )
    return requests


# ── Batch lifecycle ──────────────────────────────────────────────────────────
def submit_batch(client: anthropic.Anthropic, requests: list[Request],
                 batch_id_file: Path) -> str:
    print(f"Submitting batch with {len(requests)} requests…")
    batch = client.messages.batches.create(requests=requests)
    print(f"  Batch ID: {batch.id}")
    batch_id_file.write_text(batch.id)
    return batch.id


def poll_batch(client: anthropic.Anthropic, batch_id: str) -> None:
    print(f"Polling batch {batch_id}…")
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  Status: {batch.processing_status}  "
            f"processing={counts.processing}  "
            f"succeeded={counts.succeeded}  "
            f"errored={counts.errored}"
        )
        if batch.processing_status == "ended":
            break
        time.sleep(30)


def apply_results(
    client: anthropic.Anthropic,
    batch_id: str,
    restaurants: list[dict],
    reimprove: bool = False,
    force_confidence: Optional[str] = None,
) -> tuple[int, int]:
    applied = 0
    errors = 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            text = next(
                (b.text for b in result.result.message.content if b.type == "text"),
                "",
            )
            try:
                clean = re.sub(r"^```[a-z]*\n?", "", text.strip())
                clean = re.sub(r"\n?```$", "", clean).strip()
                data = json.loads(clean)
                r_idx, i_idx, field = _parse_custom_id(result.custom_id)
                item = restaurants[r_idx][field][i_idx]

                # Safety: never overwrite protected sources
                if item.get("source") in PROTECTED_SOURCES:
                    continue

                item["calories"] = data.get("calories")
                item["protein"] = data.get("protein")
                item["fat"] = data.get("fat")
                item["carbs"] = data.get("carbs")
                item["confidence"] = force_confidence or data.get("confidence", "low")
                item["source"] = "claude"

                if reimprove:
                    # Store extra fields from the improved prompt
                    if data.get("weight_grams_estimate"):
                        item["weight_grams_estimate"] = data["weight_grams_estimate"]
                    if data.get("reasoning"):
                        item["reasoning"] = data["reasoning"]

                applied += 1
            except Exception as exc:
                logging.error(
                    "Result parse error %s: %s | text=%s", result.custom_id, exc, text
                )
                errors += 1
        elif result.result.type == "errored":
            logging.error(
                "Batch request %s errored: %s", result.custom_id, result.result.error
            )
            errors += 1

    return applied, errors


def update_claude_md(applied: int, total: int) -> None:
    md_path = ROOT / "CLAUDE.md"
    content = md_path.read_text()
    marker = "4. Claude API КБЖУ"
    new_line = f"4. Claude API КБЖУ — ✓ ({applied}/{total} блюд обогащено, Batches API)"
    if marker in content:
        lines = content.splitlines()
        updated = [new_line if marker in line else line for line in lines]
        md_path.write_text("\n".join(updated) + "\n")
    else:
        md_path.write_text(content.rstrip() + f"\n{new_line}\n")
    print("  CLAUDE.md updated")


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="КБЖУ estimator via Claude Batches API")
    parser.add_argument(
        "--reimprove",
        action="store_true",
        help="Re-estimate source='claude' items with low/medium confidence using improved prompt",
    )
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Path to restaurants JSON file (default: data/restaurants.json)",
    )
    parser.add_argument(
        "--force-confidence",
        type=str,
        default=None,
        choices=["high", "medium", "low"],
        help="Override Claude's confidence value for all estimated items",
    )
    args = parser.parse_args()
    reimprove: bool = args.reimprove

    data_file = Path(args.file) if args.file else DATA_FILE
    backup_file = data_file.with_name(data_file.stem + "_backup.json")

    suffix = data_file.stem.replace("restaurants", "").strip("_") or "mitte"
    batch_id_file = (
        ROOT / "enrichment" / f"kbju_reimprove_{suffix}_batch_id.txt"
        if reimprove
        else ROOT / "enrichment" / f"kbju_{suffix}_batch_id.txt"
    )
    # Default files use original names for backwards compatibility
    if not args.file:
        batch_id_file = REIMPROVE_BATCH_ID_FILE if reimprove else BATCH_ID_FILE
        backup_file = BACKUP_FILE

    restaurants: list[dict] = json.loads(data_file.read_text())
    total_items = sum(
        len(r.get(f, []))
        for r in restaurants
        for f in MENU_FIELDS
    )
    print(f"Loaded {len(restaurants)} restaurants, {total_items} total menu items")

    backup_file.write_bytes(data_file.read_bytes())
    print(f"Backup saved to {backup_file}")

    client = anthropic.Anthropic()

    batch_id: Optional[str] = None

    if batch_id_file.exists():
        batch_id = batch_id_file.read_text().strip()
        print(f"Resuming from saved batch ID: {batch_id}")
    else:
        if reimprove:
            requests = collect_reimprove_requests(restaurants)
            mode_label = "reimprove (source=claude, confidence=low/medium)"
        else:
            requests = collect_requests(restaurants)
            mode_label = "first-pass (no calories)"

        print(f"Mode: {mode_label}")
        print(f"  Items to process: {len(requests)}")

        if not requests:
            print("Nothing to process.")
            return

        batch_id = submit_batch(client, requests, batch_id_file)

    poll_batch(client, batch_id)

    print("Collecting results…")
    applied, errors = apply_results(
        client, batch_id, restaurants,
        reimprove=reimprove,
        force_confidence=args.force_confidence,
    )
    print(f"  Applied: {applied}  Errors: {errors}")

    data_file.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"Saved to {data_file}")

    if batch_id_file.exists():
        batch_id_file.unlink()

    if not reimprove and not args.file:
        update_claude_md(applied, total_items)

    label = "reimproved" if reimprove else "enriched"
    print(f"\nDone. {applied} items {label}. Errors: {errors}.")


if __name__ == "__main__":
    main()
