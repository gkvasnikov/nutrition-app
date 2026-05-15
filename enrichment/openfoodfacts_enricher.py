#!/usr/bin/env python3
"""
Enrich menu items using ingredient-level lookup in Open Food Facts.

Flow:
  Phase 1 — Claude Batches API (haiku):
    For each dish with confidence != "high" and len(description) > 20,
    extract up to 5 main ingredients as JSON array.

  Phase 2 — Open Food Facts API:
    For each ingredient, fetch per-100g nutrition.
    Aggregate assuming equal-weight distribution across ingredients,
    with a default serving size of SERVING_G grams.

  Result: confidence = "medium-high", source = "openfoodfacts"

Resume: skips dishes already marked source = "openfoodfacts".

Batch ID saved to enrichment/off_batch_id.txt; deleted on success.

Usage:
  python3 enrichment/openfoodfacts_enricher.py
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import anthropic
import requests
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup_off.json"
BATCH_ID_FILE = ROOT / "enrichment" / "off_batch_id.txt"
LOG_FILE = ROOT / "enrichment.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 200
SERVING_G = 300          # assumed serving weight when no weight given
OFF_PAUSE = 1.0          # seconds between Open Food Facts requests
MIN_DESC_LEN = 20        # minimum description length to attempt enrichment

PROTECTED_SOURCES = {"official", "subway_pdf", "manual", "fatsecret", "openfoodfacts"}

OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_HEADERS = {"User-Agent": "NutritionFinderBerlin/1.0 (educational project)"}

SYSTEM_PROMPT = (
    "You are a culinary assistant. Given a dish name and description, return a JSON object with two fields:\n"
    '  "ingredients": array of up to 5 main ingredients (lowercase strings)\n'
    '  "serving_g": estimated serving weight in grams (integer)\n'
    "Return ONLY valid JSON, no markdown, no explanation.\n"
    'Example: {"ingredients": ["beef", "mashed potato", "butter"], "serving_g": 380}'
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _custom_id(r_idx: int, i_idx: int, field: str) -> str:
    f = "w" if field == "wolt_menu" else "s"
    return f"r{r_idx}-i{i_idx}-{f}"


def _parse_custom_id(custom_id: str) -> tuple[int, int, str]:
    m = re.match(r"r(\d+)-i(\d+)-([ws])", custom_id)
    if not m:
        raise ValueError(f"Bad custom_id: {custom_id}")
    field = "wolt_menu" if m.group(3) == "w" else "site_menu"
    return int(m.group(1)), int(m.group(2)), field


def needs_enrichment(item: dict) -> bool:
    if item.get("confidence") == "high":
        return False
    if item.get("source") in PROTECTED_SOURCES:
        return False
    desc = item.get("description") or ""
    return len(str(desc).strip()) > MIN_DESC_LEN


# ---------------------------------------------------------------------------
# Phase 1: Claude batch — extract ingredients
# ---------------------------------------------------------------------------

def collect_batch_requests(restaurants: list[dict]) -> list[Request]:
    requests_list = []
    for r_idx, restaurant in enumerate(restaurants):
        for field in ("wolt_menu", "site_menu"):
            for i_idx, item in enumerate(restaurant.get(field, [])):
                if not needs_enrichment(item):
                    continue
                desc = item.get("description", "")
                name = item.get("name", "")
                prompt = (
                    f"Dish name: {name}\n"
                    f"Description: {desc}\n\n"
                    'Return JSON with "ingredients" (up to 5) and "serving_g" estimate.'
                )
                requests_list.append(
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
    return requests_list


def submit_batch(client: anthropic.Anthropic, requests_list: list[Request]) -> str:
    print(f"Submitting batch: {len(requests_list)} requests…")
    batch = client.messages.batches.create(requests=requests_list)
    print(f"  Batch ID: {batch.id}")
    BATCH_ID_FILE.write_text(batch.id)
    return batch.id


def poll_batch(client: anthropic.Anthropic, batch_id: str) -> None:
    print(f"Polling batch {batch_id}…")
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        c = batch.request_counts
        print(f"  {batch.processing_status}  processing={c.processing}  succeeded={c.succeeded}  errored={c.errored}")
        if batch.processing_status == "ended":
            break
        time.sleep(30)


def collect_ingredients(
    client: anthropic.Anthropic,
    batch_id: str,
) -> dict[str, dict]:
    """Returns {custom_id: {"ingredients": [...], "serving_g": int}}"""
    results: dict[str, dict] = {}
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            continue
        raw = next(
            (b.text for b in result.result.message.content if b.type == "text"), ""
        )
        try:
            clean = re.sub(r"^```[a-z]*\n?", "", raw.strip())
            clean = re.sub(r"\n?```$", "", clean).strip()
            parsed = json.loads(clean)
            ingredients = [str(i).strip().lower() for i in parsed.get("ingredients", [])[:5]]
            serving_g = int(parsed.get("serving_g", 300))
            if ingredients:
                results[result.custom_id] = {
                    "ingredients": ingredients,
                    "serving_g": max(50, min(serving_g, 1500)),  # clamp to sane range
                }
        except Exception as exc:
            log.warning("Ingredient parse error %s: %s | raw=%s", result.custom_id, exc, raw[:100])
    return results


# ---------------------------------------------------------------------------
# Phase 2: Open Food Facts lookup
# ---------------------------------------------------------------------------

_off_cache: dict = {}  # ingredient → result (None means "not found")


def _fetch_off(ingredient: str, retries: int = 1) -> Optional[dict]:
    """Return per-100g nutrition dict for ingredient, or None. Caches results."""
    if ingredient in _off_cache:
        return _off_cache[ingredient]

    result = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                OFF_SEARCH_URL,
                params={
                    "search_terms": ingredient,
                    "json": 1,
                    "page_size": 1,
                    "fields": "product_name,nutriments",
                    "sort_by": "unique_scans_n",
                },
                headers=OFF_HEADERS,
                timeout=10,
            )
            if resp.status_code == 503:
                wait = 3 * (attempt + 1)
                log.warning("OFF 503 for %r, retrying in %ds (attempt %d)", ingredient, wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            products = resp.json().get("products", [])
            if not products:
                break
            n = products[0].get("nutriments", {})
            cal  = n.get("energy-kcal_100g") or n.get("energy_100g")
            prot = n.get("proteins_100g")
            fat  = n.get("fat_100g")
            carb = n.get("carbohydrates_100g")
            if cal is None or prot is None or fat is None or carb is None:
                break
            if "energy-kcal_100g" not in n and cal:
                cal = cal / 4.184
            result = {"calories": float(cal), "protein": float(prot),
                      "fat": float(fat), "carbs": float(carb)}
            break
        except Exception as exc:
            log.warning("OFF fetch error %r: %s", ingredient, exc)
            break

    _off_cache[ingredient] = result
    return result


def aggregate_nutrition(ingredient_nutritions: list[dict], serving_g: int) -> Optional[dict]:
    """
    Average per-100g values across ingredients → store as per-100g baseline.
    Also compute per-serving using Claude's serving_g estimate.
    """
    if not ingredient_nutritions:
        return None
    n = len(ingredient_nutritions)
    per_100g = {
        "calories": round(sum(d["calories"] for d in ingredient_nutritions) / n, 1),
        "protein":  round(sum(d["protein"]  for d in ingredient_nutritions) / n, 1),
        "fat":      round(sum(d["fat"]      for d in ingredient_nutritions) / n, 1),
        "carbs":    round(sum(d["carbs"]    for d in ingredient_nutritions) / n, 1),
    }
    scale = serving_g / 100
    per_serving = {
        "calories": round(per_100g["calories"] * scale),
        "protein":  round(per_100g["protein"]  * scale, 1),
        "fat":      round(per_100g["fat"]       * scale, 1),
        "carbs":    round(per_100g["carbs"]     * scale, 1),
    }
    return {"per_100g": per_100g, "per_serving": per_serving, "serving_g": serving_g}


def _save_merge(enriched_patches: list[tuple[int, int, str, dict]]) -> None:
    """Reload the current file and apply only OFF-enriched patches, preserving other sources."""
    try:
        current = json.loads(DATA_FILE.read_text())
    except Exception:
        return
    for r_idx, i_idx, field, patch in enriched_patches:
        try:
            current[r_idx][field][i_idx].update(patch)
        except (IndexError, KeyError):
            pass
    DATA_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=2))


def enrich_from_off(
    restaurants: list[dict],
    ingredients_map: dict[str, dict],
) -> tuple[int, int]:
    """
    For each item with extracted ingredients, look up OFF and update.
    Stores both per-100g and per-serving values.
    Returns (enriched_count, skipped_count).
    """
    enriched = skipped = 0
    total = len(ingredients_map)
    enriched_patches: list[tuple[int, int, str, dict]] = []

    for n, (custom_id, meta) in enumerate(ingredients_map.items(), 1):
        if n % 50 == 0:
            print(f"  [{n}/{total}] OFF lookups… enriched={enriched} skipped={skipped}")
            _save_merge(enriched_patches)

        r_idx, i_idx, field = _parse_custom_id(custom_id)
        item = restaurants[r_idx][field][i_idx]
        if item.get("source") in PROTECTED_SOURCES:
            skipped += 1
            continue
        ingredients = meta["ingredients"]
        serving_g   = meta["serving_g"]

        nutritions = []
        for ing in ingredients:
            result = _fetch_off(ing)
            if result:
                nutritions.append(result)
            time.sleep(OFF_PAUSE)

        if not nutritions:
            log.info("OFF no data: %s (ingredients: %s)", item.get("name"), ingredients)
            skipped += 1
            continue

        aggregated = aggregate_nutrition(nutritions, serving_g)
        if not aggregated:
            skipped += 1
            continue

        patch = {
            "calories":      aggregated["per_serving"]["calories"],
            "protein":       aggregated["per_serving"]["protein"],
            "fat":           aggregated["per_serving"]["fat"],
            "carbs":         aggregated["per_serving"]["carbs"],
            "calories_100g": aggregated["per_100g"]["calories"],
            "protein_100g":  aggregated["per_100g"]["protein"],
            "fat_100g":      aggregated["per_100g"]["fat"],
            "carbs_100g":    aggregated["per_100g"]["carbs"],
            "serving_g":     serving_g,
            "confidence":    "medium-high",
            "source":        "openfoodfacts",
        }
        item.update(patch)
        enriched_patches.append((r_idx, i_idx, field, patch))
        log.info(
            "OFF enriched: %s | serving=%dg | cal/srv=%s cal/100g=%s",
            item.get("name"), serving_g,
            aggregated["per_serving"]["calories"],
            aggregated["per_100g"]["calories"],
        )
        enriched += 1

    _save_merge(enriched_patches)
    return enriched, skipped


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    total_items = sum(
        len(r.get(f, [])) for r in restaurants for f in ("wolt_menu", "site_menu")
    )
    print(f"Loaded {len(restaurants)} restaurants, {total_items} menu items")

    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE}")

    client = anthropic.Anthropic()

    # ---- Phase 1: get ingredients via Claude batch ----
    batch_id: Optional[str] = None
    if BATCH_ID_FILE.exists():
        batch_id = BATCH_ID_FILE.read_text().strip()
        print(f"Resuming from saved batch ID: {batch_id}")
    else:
        batch_requests = collect_batch_requests(restaurants)
        eligible = len(batch_requests)
        print(f"Eligible dishes (description > {MIN_DESC_LEN} chars, not high): {eligible}")
        if not batch_requests:
            print("Nothing to enrich.")
            return
        batch_id = submit_batch(client, batch_requests)

    poll_batch(client, batch_id)

    print("Collecting ingredients from batch results…")
    ingredients_map = collect_ingredients(client, batch_id)
    print(f"  Got ingredients for {len(ingredients_map)} dishes")

    if BATCH_ID_FILE.exists():
        BATCH_ID_FILE.unlink()

    # ---- Phase 2: Open Food Facts lookups ----
    print(f"\nLooking up ingredients in Open Food Facts…")
    print(f"  Total OFF requests (up to): {sum(len(v) for v in ingredients_map.values())}")
    enriched, skipped = enrich_from_off(restaurants, ingredients_map)

    print(f"\nSaved → {DATA_FILE}")
    print(f"Enriched: {enriched}  Skipped (no OFF data): {skipped}")
    log.info("OFF run complete. enriched=%d skipped=%d", enriched, skipped)


if __name__ == "__main__":
    main()
