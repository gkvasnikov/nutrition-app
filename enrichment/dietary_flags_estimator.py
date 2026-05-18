#!/usr/bin/env python3
"""
Add dietary flags to every menu item in all_restaurants.json
using the Claude Batches API (claude-haiku-4-5).

Flags added per dish:
  is_vegan, is_vegetarian, is_gluten_free, is_diabetic_friendly,
  allergens (EU-14 list)

Resume: if enrichment/dietary_flags_batch_ids.txt exists, loads
those batch IDs, polls until done, and collects results without
resubmitting.

Usage:
  python3 enrichment/dietary_flags_estimator.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent


def _find_env() -> Path:
    candidate = ROOT
    for _ in range(6):
        if (candidate / ".env").exists():
            return candidate / ".env"
        candidate = candidate.parent
    return ROOT / ".env"


load_dotenv(_find_env(), override=True)

DATA_FILE = ROOT / "data" / "all_restaurants.json"
BACKUP_FILE = ROOT / "data" / "all_restaurants_backup_dietary.json"
BATCH_IDS_FILE = ROOT / "enrichment" / "dietary_flags_batch_ids.txt"
LOG_FILE = ROOT / "errors_dietary_flags.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 150
BATCH_SIZE = 1000
MENU_FIELDS = ["wolt_menu", "site_menu"]

SYSTEM_PROMPT = (
    "You are a culinary nutrition expert. Analyze dish information and determine "
    "dietary properties. Respond ONLY with valid JSON, no markdown, no explanation."
)

USER_PROMPT_TEMPLATE = """\
Dish: {name}
Description: {description}
Nutrition per serving: {calories} kcal, protein {protein}g, fat {fat}g, carbs {carbs}g, weight ~{weight}g

Respond ONLY with JSON (no markdown):
{{
  "is_vegan": true/false/null,
  "is_vegetarian": true/false/null,
  "is_gluten_free": true/false/null,
  "is_diabetic_friendly": true/false/null,
  "allergens": ["gluten", "dairy", ...]
}}

Use null when you cannot determine with reasonable confidence.
EU-14 allergens only: gluten, crustaceans, eggs, fish, peanuts, soybeans, dairy, nuts, celery, mustard, sesame, sulphites, lupin, molluscs.
For is_diabetic_friendly: true only if carbs <= 30g, no sugary/dessert keywords, no ultra-refined carb keywords. Use null if nutritional data is uncertain."""


# ── ID helpers ───────────────────────────────────────────────────────────────

def _custom_id(r_idx: int, i_idx: int, field: str) -> str:
    src = "w" if field == "wolt_menu" else "s"
    return f"r{r_idx}-{src}-i{i_idx}"


def _parse_custom_id(cid: str) -> tuple:
    m = re.match(r"r(\d+)-([ws])-i(\d+)", cid)
    if not m:
        raise ValueError(f"Unparseable custom_id: {cid}")
    field = "wolt_menu" if m.group(2) == "w" else "site_menu"
    return int(m.group(1)), int(m.group(3)), field


# ── Item helpers ─────────────────────────────────────────────────────────────

def _needs_flags(item: dict) -> bool:
    return "is_vegan" not in item


def _build_prompt(item: dict) -> str:
    return USER_PROMPT_TEMPLATE.format(
        name=item.get("name", ""),
        description=item.get("description") or "",
        calories=item.get("calories") if item.get("calories") is not None else "?",
        protein=item.get("protein") if item.get("protein") is not None else "?",
        fat=item.get("fat") if item.get("fat") is not None else "?",
        carbs=item.get("carbs") if item.get("carbs") is not None else "?",
        weight=item.get("weight_grams_estimate") or "?",
    )


# ── Request collection ────────────────────────────────────────────────────────

def collect_requests(restaurants: list) -> list:
    requests = []
    for r_idx, restaurant in enumerate(restaurants):
        for field in MENU_FIELDS:
            for i_idx, item in enumerate(restaurant.get(field, [])):
                if not _needs_flags(item):
                    continue
                requests.append(Request(
                    custom_id=_custom_id(r_idx, i_idx, field),
                    params=MessageCreateParamsNonStreaming(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        messages=[{"role": "user", "content": _build_prompt(item)}],
                    ),
                ))
    return requests


# ── Batch lifecycle ───────────────────────────────────────────────────────────

def submit_batch(client: anthropic.Anthropic, chunk: list) -> str:
    batch = client.messages.batches.create(requests=chunk)
    print(f"  Submitted {batch.id} ({len(chunk)} requests)")
    return batch.id


def poll_batch(client: anthropic.Anthropic, batch_id: str) -> None:
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  {batch_id}: {batch.processing_status} | "
            f"processing={counts.processing} "
            f"succeeded={counts.succeeded} "
            f"errored={counts.errored}"
        )
        if batch.processing_status == "ended":
            break
        time.sleep(30)


def apply_batch_results(
    client: anthropic.Anthropic,
    batch_id: str,
    restaurants: list,
) -> tuple:
    applied = errors = 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            log.error(
                "Batch %s request %s errored: %s",
                batch_id, result.custom_id, result.result.error,
            )
            errors += 1
            continue

        text = next(
            (b.text for b in result.result.message.content if b.type == "text"), ""
        )
        try:
            clean = re.sub(r"^```[a-z]*\n?", "", text.strip())
            clean = re.sub(r"\n?```$", "", clean).strip()
            data = json.loads(clean)
        except Exception as exc:
            log.error(
                "JSON parse error %s: %s | text=%s",
                result.custom_id, exc, text[:300],
            )
            data = {}

        try:
            r_idx, i_idx, field = _parse_custom_id(result.custom_id)
            item = restaurants[r_idx][field][i_idx]
        except Exception as exc:
            log.error("ID parse error %s: %s", result.custom_id, exc)
            errors += 1
            continue

        # Skip if already flagged (idempotent resume)
        if not _needs_flags(item):
            continue

        item["is_vegan"] = data.get("is_vegan")
        item["is_vegetarian"] = data.get("is_vegetarian")
        item["is_gluten_free"] = data.get("is_gluten_free")
        item["allergens"] = data.get("allergens") or []

        # Low nutritional confidence → diabetic_friendly must be null
        df = data.get("is_diabetic_friendly")
        if item.get("confidence") == "low":
            df = None
        item["is_diabetic_friendly"] = df

        applied += 1

    return applied, errors


# ── I/O ───────────────────────────────────────────────────────────────────────

def save(restaurants: list, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _load_batch_ids() -> list:
    if not BATCH_IDS_FILE.exists():
        return []
    return [l.strip() for l in BATCH_IDS_FILE.read_text().splitlines() if l.strip()]


def _save_batch_ids(batch_ids: list) -> None:
    BATCH_IDS_FILE.write_text("\n".join(batch_ids) + "\n")


# ── Statistics ────────────────────────────────────────────────────────────────

def print_stats(restaurants: list) -> None:
    total = vegan = vegetarian = gf = diabetic = 0
    allergen_counter: Counter = Counter()

    for r in restaurants:
        for field in MENU_FIELDS:
            for item in r.get(field, []):
                if "is_vegan" not in item:
                    continue
                total += 1
                if item.get("is_vegan") is True:
                    vegan += 1
                if item.get("is_vegetarian") is True:
                    vegetarian += 1
                if item.get("is_gluten_free") is True:
                    gf += 1
                if item.get("is_diabetic_friendly") is True:
                    diabetic += 1
                for allergen in (item.get("allergens") or []):
                    allergen_counter[allergen] += 1

    def pct(n: int) -> str:
        return f"{n / total * 100:.1f}%" if total else "0%"

    print(f"\n{'─' * 48}")
    print(f"  Dishes with flags:      {total:,}")
    print(f"  Vegan:                  {vegan:,} ({pct(vegan)})")
    print(f"  Vegetarian:             {vegetarian:,} ({pct(vegetarian)})")
    print(f"  Gluten-free:            {gf:,} ({pct(gf)})")
    print(f"  Diabetic-friendly:      {diabetic:,} ({pct(diabetic)})")
    if allergen_counter:
        print(f"\n  Top-5 allergens:")
        for allergen, count in allergen_counter.most_common(5):
            print(f"    {allergen:<16} {count:,}")
    print(f"{'─' * 48}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY not set — check .env")

    print(f"Loading {DATA_FILE}…")
    restaurants = json.loads(DATA_FILE.read_text())
    total_items = sum(len(r.get(f, [])) for r in restaurants for f in MENU_FIELDS)
    print(f"  {len(restaurants)} restaurants, {total_items:,} menu items")

    BACKUP_FILE.write_bytes(DATA_FILE.read_bytes())
    print(f"  Backup → {BACKUP_FILE.name}")

    client = anthropic.Anthropic()

    # ── Resume: existing batch IDs ────────────────────────────────────────────
    saved_ids = _load_batch_ids()
    if saved_ids:
        print(f"\nResuming: {len(saved_ids)} saved batch(es)")
        total_applied = total_errors = 0
        for i, bid in enumerate(saved_ids, 1):
            print(f"\nBatch {i}/{len(saved_ids)}: polling {bid}…")
            poll_batch(client, bid)
            applied, errors = apply_batch_results(client, bid, restaurants)
            total_applied += applied
            total_errors += errors
            print(f"  Applied: {applied}  Errors: {errors}")
            save(restaurants, DATA_FILE)
            print(f"  Saved checkpoint")
        BATCH_IDS_FILE.unlink(missing_ok=True)
        print(f"\nResume complete — applied: {total_applied}  errors: {total_errors}")
        print_stats(restaurants)
        return

    # ── Fresh run ─────────────────────────────────────────────────────────────
    print("\nCollecting items needing dietary flags…")
    all_requests = collect_requests(restaurants)
    needs = len(all_requests)
    already = total_items - needs
    print(f"  To process: {needs:,}  Already done: {already:,}")

    if not all_requests:
        print("Nothing to process.")
        print_stats(restaurants)
        return

    chunks = [all_requests[i:i + BATCH_SIZE] for i in range(0, len(all_requests), BATCH_SIZE)]
    print(f"  Splitting into {len(chunks)} batch(es) of ≤{BATCH_SIZE}\n")

    batch_ids = []
    for i, chunk in enumerate(chunks, 1):
        print(f"Submitting batch {i}/{len(chunks)}…")
        bid = submit_batch(client, chunk)
        batch_ids.append(bid)
        _save_batch_ids(batch_ids)  # persist after each submit

    # ── Poll + collect each batch ─────────────────────────────────────────────
    total_applied = total_errors = 0
    for i, bid in enumerate(batch_ids, 1):
        print(f"\nPolling batch {i}/{len(batch_ids)}: {bid}…")
        poll_batch(client, bid)

        print(f"  Collecting results…")
        applied, errors = apply_batch_results(client, bid, restaurants)
        total_applied += applied
        total_errors += errors
        print(f"  Applied: {applied}  Errors: {errors}")

        save(restaurants, DATA_FILE)
        print(f"  Saved after batch {i}/{len(batch_ids)}")

    BATCH_IDS_FILE.unlink(missing_ok=True)
    print(f"\nDone — total applied: {total_applied}  errors: {total_errors}")
    print_stats(restaurants)


if __name__ == "__main__":
    main()
