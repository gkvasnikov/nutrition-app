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
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)

app = Flask(__name__, static_folder=None)

SYSTEM = (
    "You are a nutrition advisor for a Berlin restaurant discovery app. "
    "Given a dish name, optional description, and nutritional data, return three things in Russian:\n"
    "1. what — one short sentence explaining what this dish is (use your food knowledge; "
    "if the name is in another language like Turkish or German, explain it clearly in Russian). "
    "Keep it under 15 words.\n"
    "2. rating — one word from: Ограниченно, Нормально, Хорошо, Питательно\n"
    "3. advice — 2-3 sentences specific to the actual nutrients shown.\n"
    "Return ONLY valid JSON: "
    '{"what": "<text>", "rating": "...", "advice": "<text>"}'
)

RATING_RULES = """
Rating guide (choose the most fitting):
- Питательно: protein > 25g AND reasonable calories (< 700 kcal)
- Хорошо: protein > 15g OR well-balanced macros
- Нормально: average restaurant dish
- Ограниченно: very high fat/calories with low protein, or very small portion
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

    def fmt(v, unit="г"):
        return f"{v}{unit}" if v is not None else "н/д"

    description = d.get("description", "")
    prompt = (
        f"Dish: {name}\n"
        + (f"Description: {description}\n" if description else "")
        + f"Restaurant: {restaurant}" + (f" ({cuisine} cuisine)" if cuisine else "") + "\n"
        f"Per serving (~{fmt(weight)}):\n"
        f"  Calories: {fmt(calories, ' kcal')}\n"
        f"  Protein:  {fmt(protein)}\n"
        f"  Fat:      {fmt(fat)}\n"
        f"  Carbs:    {fmt(carbs)}\n"
        f"\n{RATING_RULES}"
    )

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=200,
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
