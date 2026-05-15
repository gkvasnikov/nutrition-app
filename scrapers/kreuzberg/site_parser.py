"""
Парсит сайты из data/kreuzberg/sites_with_kbju.json и сохраняет меню в
restaurants_kreuzberg.json.

Два режима по полю site_has_pdf:
  A) PDF  — скачивает site_kbju_url, извлекает текст pdfplumber → Claude haiku
  B) HTML — открывает site_kbju_url через Playwright → Claude haiku

Использование:
  python3 scrapers/kreuzberg/site_parser.py
  python3 scrapers/kreuzberg/site_parser.py --resume
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import anthropic
import pdfplumber
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# ── Пути ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env", override=True)

SITES_FILE    = ROOT / "data" / "kreuzberg" / "sites_with_kbju.json"
RESTAURANTS_FILE = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
ERRORS_LOG    = ROOT / "errors.log"

# ── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    filename=str(ERRORS_LOG),
    level=logging.ERROR,
    format="%(asctime)s  site_parser  %(message)s",
)

# ── Константы ───────────────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NutritionBot/1.0)"}
PAUSE_S = 3.0
MAX_TEXT = 14000  # символов → Claude haiku

client = anthropic.Anthropic()

EXTRACT_PROMPT = """\
Извлеки все блюда с КБЖУ из этого меню.
Верни JSON: [{{"name": "...", "calories": число|null, "protein": число|null, \
"fat": число|null, "carbs": число|null, "weight_grams": число|null}}]
Только JSON, без пояснений.

{text}"""


# ── Утилиты ─────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── Шаг 1: извлечение текста ────────────────────────────────────────────────

def fetch_pdf_text(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    tmp = Path("/tmp/_kreuzberg_site_menu.pdf")
    tmp.write_bytes(resp.content)
    text = ""
    with pdfplumber.open(tmp) as pdf:
        for pg in pdf.pages[:30]:
            text += (pg.extract_text() or "") + "\n"
    return text[:MAX_TEXT]


def fetch_html_text(page, url: str) -> str:
    page.goto(url, wait_until="networkidle", timeout=35000)
    page.wait_for_load_state("networkidle")
    return page.inner_text("body")[:MAX_TEXT]


# ── Шаг 2: Claude haiku ─────────────────────────────────────────────────────

def extract_menu_claude(text: str) -> list[dict]:
    if not text.strip():
        return []
    msg = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=8192,
        messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
    )
    raw = msg.content[0].text
    raw = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    # Если JSON обрезан — закрыть массив
    last_brace = raw.rfind("}")
    if last_brace != -1 and not raw.rstrip().endswith("]"):
        raw = raw[: last_brace + 1] + "\n]"
    return json.loads(raw)


def has_kbju(items: list[dict]) -> bool:
    return any(
        it.get("calories") is not None
        or it.get("protein") is not None
        for it in items
    )


# ── Main ────────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    sites = load_json(SITES_FILE)
    restaurants = load_json(RESTAURANTS_FILE)
    rest_by_id = {r["place_id"]: r for r in restaurants}

    total = len(sites)
    ok = 0
    total_dishes = 0
    errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pw_page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        pw_page.set_extra_http_headers({"Accept-Language": "de-DE,de;q=0.9"})

        for i, site in enumerate(sites, 1):
            pid = site.get("place_id") or site.get("restaurant_id")
            r = rest_by_id.get(pid)
            if r is None:
                continue

            # Resume: пропускаем уже обработанные
            if resume and r.get("kbju_status") not in ("no_data", None):
                continue

            is_pdf = site.get("site_has_pdf", site.get("has_pdf", False))
            url = site.get("site_kbju_url") or site.get("url", "")
            if not url:
                continue

            mode = "PDF" if is_pdf else "HTML"
            print(f"[{i}/{total}] {r['name']} ({mode}) → {url[:60]}", end=" ", flush=True)

            try:
                if is_pdf:
                    text = fetch_pdf_text(url)
                else:
                    text = fetch_html_text(pw_page, url)

                items = extract_menu_claude(text)

                if not items:
                    print("нет блюд")
                    r["kbju_status"] = "no_data"
                elif not has_kbju(items):
                    print(f"{len(items)} блюд (без КБЖУ)")
                    r["kbju_status"] = "no_data"
                    r["site_menu_no_kbju"] = items
                else:
                    kbju_count = sum(1 for it in items if it.get("calories") is not None)
                    print(f"{len(items)} блюд, {kbju_count} с КБЖУ ✓")
                    r["menu"] = items
                    r["source_url"] = url
                    r["kbju_status"] = "verified_pdf" if is_pdf else "verified_site"
                    ok += 1
                    total_dishes += len(items)

            except Exception as e:
                print(f"ОШИБКА: {e}")
                logging.error("%s | %s | %s", r["name"], url, e)
                errors += 1

            save_json(RESTAURANTS_FILE, restaurants)
            time.sleep(PAUSE_S)

        browser.close()

    print(f"\n{'─' * 50}")
    print(f"Всего сайтов           : {total}")
    print(f"Успешно спаршено       : {ok}")
    print(f"Блюд с КБЖУ получено   : {total_dishes}")
    print(f"Ошибок                 : {errors}")
    print(f"Файл                   : {RESTAURANTS_FILE}")


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
