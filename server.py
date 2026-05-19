"""
Flask-сервер: отдаёт статику фронтенда + /api/advice для AI-совета по блюду.

Запуск:
  python3 server.py
  Открывать: http://localhost:8080/frontend/index.html
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import anthropic
import requests as http_requests
from dotenv import load_dotenv
from filelock import FileLock
from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

app = Flask(__name__, static_folder=None)

PHOTO_CACHE_FILE = ROOT / "data" / "photo_cache.json"
PHOTO_CACHE_LOCK = FileLock(str(PHOTO_CACHE_FILE) + ".lock")

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


def _load_cache() -> dict:
    if PHOTO_CACHE_FILE.exists():
        try:
            return json.loads(PHOTO_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    tmp = PHOTO_CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    tmp.replace(PHOTO_CACHE_FILE)


def _is_valid_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith("http")

SYSTEM = (
    "You are a nutrition advisor for a Berlin restaurant discovery app. "
    "Given a dish and the user's nutrition goals (or none if not set), return JSON with:\n"
    "1. score — integer 0–100: how well this dish fits the user's goals "
    "(if no goals: rate overall nutritional quality)\n"
    "2. label — exactly one word matching the score: "
    "Poor (0–40), Fair (41–65), Good (66–85), Excellent (86–100)\n"
    "3. what — one short English sentence (≤15 words) explaining what this dish is; "
    "translate foreign names (Turkish, German, etc.) into plain English\n"
    "4. advice — 2–3 sentences explaining the score: what fits the goals and what doesn't\n"
    "Return ONLY valid JSON: "
    '{"score": <0-100>, "label": "Poor|Fair|Good|Excellent", "what": "<text>", "advice": "<text>"}'
)

SCORING_GUIDE = """
Scoring rules (start at 100, deduct, clamp 0–100):
- Calories below range minimum: −20 pts
- Calories above range maximum: −20 pts
- Protein below minimum goal: −25 pts (proportional to shortfall)
- Fat above maximum goal: −20 pts (proportional to excess)
- Carbs above maximum goal: −15 pts (proportional to excess)
- If no user goals set: score overall nutritional quality
  (high protein + reasonable calories = higher score;
   very high fat+calories with low protein = lower score)
- label MUST match score band: Poor 0–40, Fair 41–65, Good 66–85, Excellent 86–100
"""


@app.route("/")
@app.route("/frontend/")
def index():
    return send_from_directory(ROOT / "frontend", "index.html")


@app.route("/frontend/<path:filename>")
def frontend_static(filename):
    return send_from_directory(ROOT / "frontend", filename)


@app.route("/data/<path:filename>")
def data_static(filename):
    return send_from_directory(ROOT / "data", filename)


@app.route("/api/config")
def config():
    return jsonify({
        "gmaps_key": os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_PLACES_API_KEY", ""),
    })


@app.route("/api/dish-photo")
def dish_photo():
    dish_name = request.args.get("name", "").strip()
    if not dish_name:
        return jsonify({"url": None}), 400

    # Step 1: check cache
    with PHOTO_CACHE_LOCK:
        cache = _load_cache()

    if dish_name in cache:
        cached_url = cache[dish_name]
        # Step 2: HEAD-check cached URL (3s timeout)
        try:
            resp = http_requests.head(cached_url, timeout=3, allow_redirects=True)
            if resp.status_code == 200:
                return jsonify({"url": cached_url})
        except Exception:
            pass
        # URL is dead — evict from cache, fall through to Pexels
        with PHOTO_CACHE_LOCK:
            cache = _load_cache()
            cache.pop(dish_name, None)
            _save_cache(cache)

    # Step 3: search Pexels
    pexels_key = os.environ.get("PEXELS_API_KEY", "")
    if not pexels_key:
        return jsonify({"url": None, "error": "no_key"})

    try:
        resp = http_requests.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": pexels_key},
            params={
                "query":       f"{dish_name} food",
                "per_page":    1,
                "orientation": "landscape",
            },
            timeout=8,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            return jsonify({"url": None, "error": "no_photos"})

        url = photos[0].get("src", {}).get("large", "")
        if not _is_valid_url(url):
            return jsonify({"url": None, "error": "invalid_url"})

        # Step 5: save to cache
        with PHOTO_CACHE_LOCK:
            cache = _load_cache()
            cache[dish_name] = url
            _save_cache(cache)

        return jsonify({"url": url})

    except Exception as e:
        return jsonify({"url": None, "error": str(e)})


@app.route("/api/advice", methods=["POST"])
def advice():
    d = request.get_json(force=True)
    name       = d.get("name", "")
    restaurant = d.get("restaurant", "")
    cuisine    = d.get("cuisine", "")
    calories   = d.get("calories")
    protein    = d.get("protein")
    fat        = d.get("fat")
    carbs      = d.get("carbs")
    weight     = d.get("weight")
    description = d.get("description", "")

    # User filter goals (None = not set / default range)
    cal_min  = d.get("cal_min")
    cal_max  = d.get("cal_max")
    prot_min = d.get("prot_min")
    fat_max  = d.get("fat_max")
    carb_max = d.get("carb_max")

    def fmt(v, unit="g"):
        return f"{v}{unit}" if v is not None else "n/a"

    # Build goals line
    has_goals = any(x is not None for x in [cal_min, cal_max, prot_min, fat_max, carb_max])
    if has_goals:
        goal_parts = []
        if cal_min is not None: goal_parts.append(f"calories ≥ {cal_min} kcal")
        if cal_max is not None: goal_parts.append(f"calories ≤ {cal_max} kcal")
        if prot_min is not None: goal_parts.append(f"protein ≥ {prot_min}g")
        if fat_max  is not None: goal_parts.append(f"fat ≤ {fat_max}g")
        if carb_max is not None: goal_parts.append(f"carbs ≤ {carb_max}g")
        goals_line = "User's meal goal: " + ", ".join(goal_parts)
    else:
        goals_line = "User's meal goal: none set — score overall nutritional quality"

    prompt = (
        f"{goals_line}\n\n"
        f"Dish: {name}\n"
        + (f"Description: {description}\n" if description else "")
        + f"Restaurant: {restaurant}" + (f" ({cuisine} cuisine)" if cuisine else "") + "\n"
        f"Per serving (~{fmt(weight)}):\n"
        f"  Calories: {fmt(calories, ' kcal')}\n"
        f"  Protein:  {fmt(protein)}\n"
        f"  Fat:      {fmt(fat)}\n"
        f"  Carbs:    {fmt(carbs)}\n"
        f"\n{SCORING_GUIDE}"
    )

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=220,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text
    clean = re.sub(r"^```[a-z]*\n?|\n?```$", "", text.strip())
    data = json.loads(clean)
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Nutrition App server → http://localhost:{port}/frontend/index.html")
    app.run(host="0.0.0.0", port=port, debug=False)
