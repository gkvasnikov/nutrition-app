#!/usr/bin/env python3
"""
Enrich menu items with FatSecret API data.

For every dish where confidence != "high" and source not in
{"official", "subway_pdf", "manual", "fatsecret"}:
  1. Search FatSecret by dish name
  2. Fuzzy-match best result (threshold 0.70)
  3. On match: update calories/protein/fat/carbs, set confidence="high", source="fatsecret"
  4. On no match: leave untouched

Resume: skips dishes already sourced from "fatsecret".

Usage:
  export FATSECRET_CLIENT_ID=...
  export FATSECRET_CLIENT_SECRET=...
  python3 enrichment/fatsecret_enricher.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

import requests
from fuzzywuzzy import fuzz

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup_fatsecret.json"
LOG_FILE = ROOT / "enrichment.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

FATSECRET_TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
FATSECRET_API_URL = "https://platform.fatsecret.com/rest/server.api"

FUZZY_THRESHOLD = 70       # 0–100, require ≥70 to accept a match
PAUSE = 0.5                # seconds between API requests

# Sources that hold authoritative data — never overwrite
PROTECTED_SOURCES = {"official", "subway_pdf", "manual", "fatsecret"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_token_cache: dict = {}


def get_token(client_id: str, client_secret: str) -> str:
    """Fetch or reuse an OAuth2 access token."""
    if _token_cache.get("token") and time.time() < _token_cache.get("expires_at", 0) - 30:
        return _token_cache["token"]

    resp = requests.post(
        FATSECRET_TOKEN_URL,
        data={"grant_type": "client_credentials", "scope": "basic"},
        auth=(client_id, client_secret),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 86400)
    return _token_cache["token"]


# ---------------------------------------------------------------------------
# FatSecret search
# ---------------------------------------------------------------------------

def _api_get(token: str, params: dict) -> dict:
    params = {"format": "json", **params}
    resp = requests.get(
        FATSECRET_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_description(desc: str) -> Optional[dict]:
    """
    Parse FatSecret food_description string, e.g.:
    "Per 100g - Calories: 150kcal | Fat: 5.00g | Carbs: 20.00g | Protein: 8.00g"
    Returns dict with calories/protein/fat/carbs or None on failure.
    """
    patterns = {
        "calories": r"Calories:\s*([\d.]+)\s*kcal",
        "fat":      r"Fat:\s*([\d.]+)\s*g",
        "carbs":    r"Carbs:\s*([\d.]+)\s*g",
        "protein":  r"Protein:\s*([\d.]+)\s*g",
    }
    result = {}
    for key, pat in patterns.items():
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            result[key] = float(m.group(1))
    if len(result) == 4:
        return result
    return None


def search_food(token: str, query: str) -> Optional[tuple[str, dict]]:
    """
    Search FatSecret for `query`. Returns (matched_name, nutrition_dict)
    for the best result above FUZZY_THRESHOLD, or None.
    """
    try:
        data = _api_get(token, {"method": "foods.search", "search_expression": query, "max_results": 5})
    except requests.HTTPError as exc:
        log.warning("FatSecret search error for %r: %s", query, exc)
        return None

    foods_block = data.get("foods", {})
    foods = foods_block.get("food", [])
    if isinstance(foods, dict):
        foods = [foods]  # API returns single item as dict, not list

    best_score = 0
    best_name = None
    best_nutrition = None

    query_lower = query.lower()
    for food in foods:
        name = food.get("food_name", "")
        score = fuzz.token_set_ratio(query_lower, name.lower())
        if score > best_score:
            nutrition = _parse_description(food.get("food_description", ""))
            if nutrition:
                best_score = score
                best_name = name
                best_nutrition = nutrition

    if best_score >= FUZZY_THRESHOLD and best_nutrition:
        return best_name, best_nutrition
    return None


def lookup(token: str, dish_name: str) -> Optional[tuple[str, dict]]:
    """
    Try full name first, then first 3 words as fallback.
    """
    result = search_food(token, dish_name)
    if result:
        return result

    words = dish_name.split()
    if len(words) > 3:
        short = " ".join(words[:3])
        time.sleep(PAUSE)
        result = search_food(token, short)
        if result:
            return result

    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def needs_enrichment(item: dict) -> bool:
    if item.get("confidence") == "high":
        return False
    if item.get("source") in PROTECTED_SOURCES:
        return False
    return True


def main() -> None:
    client_id = os.environ.get("FATSECRET_CLIENT_ID", "").strip()
    client_secret = os.environ.get("FATSECRET_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit(
            "Set FATSECRET_CLIENT_ID and FATSECRET_CLIENT_SECRET env vars before running."
        )

    restaurants: list[dict] = json.loads(DATA_FILE.read_text())

    # Count targets
    targets = [
        (r_idx, field, i_idx, item)
        for r_idx, r in enumerate(restaurants)
        for field in ("wolt_menu", "site_menu")
        for i_idx, item in enumerate(r.get(field, []))
        if needs_enrichment(item)
    ]

    print(f"Loaded {len(restaurants)} restaurants")
    print(f"Dishes to enrich: {len(targets)}")

    if not targets:
        print("Nothing to do.")
        return

    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE}\n")

    token = get_token(client_id, client_secret)
    print("FatSecret token obtained.\n")

    found = not_found = errors = 0

    for n, (r_idx, field, i_idx, item) in enumerate(targets, 1):
        name = item.get("name", "")
        if not name:
            not_found += 1
            continue

        if n % 100 == 0:
            print(f"  [{n}/{len(targets)}] found={found} not_found={not_found} errors={errors}")
            # Refresh token if needed
            token = get_token(client_id, client_secret)

        try:
            result = lookup(token, name)
        except Exception as exc:
            log.error("Lookup error %r: %s", name, exc)
            errors += 1
            time.sleep(PAUSE)
            continue

        if result:
            matched_name, nutrition = result
            item["calories"] = round(nutrition["calories"])
            item["protein"]  = round(nutrition["protein"], 1)
            item["fat"]      = round(nutrition["fat"], 1)
            item["carbs"]    = round(nutrition["carbs"], 1)
            item["confidence"] = "high"
            item["source"] = "fatsecret"
            log.info("MATCH %r → %r", name, matched_name)
            found += 1
        else:
            log.info("NO MATCH %r", name)
            not_found += 1

        # Save every 50 dishes to preserve progress
        if n % 50 == 0:
            DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))

        time.sleep(PAUSE)

    DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
    print(f"\nDone.")
    print(f"  Matched:   {found}")
    print(f"  No match:  {not_found}")
    print(f"  Errors:    {errors}")
    print(f"  Total:     {len(targets)}")
    log.info("Run complete. found=%d not_found=%d errors=%d", found, not_found, errors)


if __name__ == "__main__":
    main()
