"""
Сканирует сайты ресторанов из restaurants_kreuzberg.json на наличие КБЖУ.
Только requests, без Playwright.

Использование:
  python3 scrapers/kreuzberg/site_detector.py
  python3 scrapers/kreuzberg/site_detector.py --resume
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ── Пути ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent
RESTAURANTS_FILE = ROOT / "data" / "kreuzberg" / "restaurants_kreuzberg.json"
SITES_FILE       = ROOT / "data" / "kreuzberg" / "sites_with_kbju.json"

# ── Константы ──────────────────────────────────────────────────────────────

PAUSE_S = 1.0
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

KBJU_SIGNALS = [
    "kcal", "kalorien", "kalorie", "kj", "brennwert",
    "nährwert", "nährwerte", "nutrition", "calories",
    "protein", "fett", "kohlenhydrate", "eiweiß",
]

PDF_MENU_KEYWORDS = [
    "menu", "speisekarte", "karte", "nutrition", "nährwert",
]


# ── Утилиты ─────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list:
    return json.loads(path.read_text()) if path.exists() else []


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def count_kbju_signals(text: str) -> int:
    text_l = text.lower()
    return sum(1 for kw in KBJU_SIGNALS if kw in text_l)


def find_pdf_menu_link(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """Ищет <a href="...pdf..."> рядом с текстом меню/нährwert."""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" not in href.lower():
            continue
        link_text = (a.get_text() + " " + href).lower()
        if any(kw in link_text for kw in PDF_MENU_KEYWORDS):
            return urljoin(base_url, href)
    return None


def find_kbju_page(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    """
    Если КБЖУ сигналы найдены на главной — возвращает base_url.
    Иначе ищет ссылку на страницу меню/nutrition и проверяет её.
    """
    # Сначала проверяем подстраницы с ключевыми словами
    for a in soup.find_all("a", href=True):
        href_l = a["href"].lower()
        text_l = a.get_text().lower()
        if any(kw in href_l or kw in text_l for kw in PDF_MENU_KEYWORDS):
            return urljoin(base_url, a["href"])
    return base_url


# ── Сканирование одного сайта ───────────────────────────────────────────────

def scan_site(website: str) -> dict:
    """
    Возвращает dict с полями:
      site_kbju_signal, site_kbju_url, site_has_pdf, site_status
    """
    result = {
        "site_kbju_signal": False,
        "site_kbju_url": None,
        "site_has_pdf": False,
        "site_status": "ok",
    }

    try:
        resp = requests.get(website, headers=HEADERS, timeout=8, allow_redirects=True)
        if resp.status_code != 200:
            result["site_status"] = f"http_{resp.status_code}"
            return result
    except requests.Timeout:
        result["site_status"] = "timeout"
        return result
    except Exception as e:
        result["site_status"] = f"error: {type(e).__name__}"
        return result

    soup = BeautifulSoup(resp.text, "html.parser")
    body_text = soup.get_text(" ", strip=True)

    # Проверяем КБЖУ сигналы на главной странице
    signal_count = count_kbju_signals(body_text)

    # Ищем PDF со словами меню/nutrition
    pdf_url = find_pdf_menu_link(soup, website)

    if signal_count >= 2 or pdf_url:
        result["site_kbju_signal"] = True
        result["site_has_pdf"] = bool(pdf_url)
        result["site_kbju_url"] = pdf_url if pdf_url else website
    else:
        # Пробуем найти подстраницу меню и проверить её
        sub_url = find_kbju_page(soup, website)
        if sub_url and sub_url != website:
            try:
                resp2 = requests.get(sub_url, headers=HEADERS, timeout=8, allow_redirects=True)
                if resp2.status_code == 200:
                    soup2 = BeautifulSoup(resp2.text, "html.parser")
                    body2 = soup2.get_text(" ", strip=True)
                    signal_count2 = count_kbju_signals(body2)
                    pdf_url2 = find_pdf_menu_link(soup2, sub_url)
                    if signal_count2 >= 2 or pdf_url2:
                        result["site_kbju_signal"] = True
                        result["site_has_pdf"] = bool(pdf_url2)
                        result["site_kbju_url"] = pdf_url2 if pdf_url2 else sub_url
            except Exception:
                pass

    return result


# ── Main ────────────────────────────────────────────────────────────────────

def run(resume: bool) -> None:
    restaurants = load_json(RESTAURANTS_FILE)

    targets = [
        r for r in restaurants
        if r.get("kbju_status") == "no_data" and r.get("website")
    ]

    if resume:
        targets = [r for r in targets if "site_kbju_signal" not in r]

    print(f"Сайтов для сканирования: {len(targets)}")

    checked = 0
    found_signal = 0
    found_pdf = 0

    for i, r in enumerate(targets, 1):
        website = r["website"]
        print(f"[{i}/{len(targets)}] {r['name']} → {website[:55]}", end=" ", flush=True)

        result = scan_site(website)
        r.update(result)
        checked += 1

        if result["site_kbju_signal"]:
            found_signal += 1
            if result["site_has_pdf"]:
                found_pdf += 1
            tag = "PDF" if result["site_has_pdf"] else "HTML"
            print(f"✓ {tag}  {result['site_kbju_url'][:50] if result['site_kbju_url'] else ''}")
        else:
            print(result["site_status"])

        # Сохраняем после каждой записи
        save_json(RESTAURANTS_FILE, restaurants)
        time.sleep(PAUSE_S)

    # Собираем sites_with_kbju.json
    with_signal = [
        {
            "place_id": r["place_id"],
            "name": r["name"],
            "website": r.get("website", ""),
            "site_kbju_url": r.get("site_kbju_url"),
            "site_has_pdf": r.get("site_has_pdf", False),
        }
        for r in restaurants
        if r.get("site_kbju_signal")
    ]
    save_json(SITES_FILE, with_signal)

    # Итог
    print(f"\n{'─' * 50}")
    print(f"Проверено сайтов       : {checked}")
    print(f"Найдено КБЖУ сигналов  : {found_signal}")
    print(f"Из них с PDF           : {found_pdf}")
    print(f"Файл кандидатов        : {SITES_FILE}")

    if with_signal:
        print(f"\nТоп-{min(20, len(with_signal))} с сигналами:")
        for e in with_signal[:20]:
            tag = "[PDF]" if e["site_has_pdf"] else "[HTML]"
            print(f"  {tag} {e['name'][:35]:<35} {(e['site_kbju_url'] or '')[:55]}")


if __name__ == "__main__":
    run(resume="--resume" in sys.argv)
