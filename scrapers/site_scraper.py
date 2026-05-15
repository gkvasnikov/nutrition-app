#!/usr/bin/env python3
"""
Scrape menu text from restaurant websites and extract dishes via Claude API.

Flow per restaurant (has website, no wolt_menu, no site_menu):
  1. Fetch homepage, search for menu-page links
  2. Try common path suffixes (/menu /speisekarte /karte /essen)
  3. Extract clean text from the best candidate page
  4. Ask claude-haiku-4-5 to parse dishes → [{name, description, price}]
  5. Save result to restaurants.json under field "site_menu"

Usage:
  python3 scrapers/site_scraper.py
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import io

import anthropic
import pdfplumber
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent
DATA_FILE = ROOT / "data" / "restaurants.json"
BACKUP_FILE = ROOT / "data" / "restaurants_backup_site.json"

logging.basicConfig(
    filename=ROOT / "errors.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

MENU_KEYWORDS = ["menu", "speisekarte", "karte", "essen", "gerichte", "food", "carta"]
MENU_PATHS = ["/menu", "/speisekarte", "/karte", "/essen", "/food", "/gerichte"]
MIN_TEXT_LEN = 150
MAX_TEXT_LEN = 12_000
REQUEST_TIMEOUT = 10
PAUSE = 2.0

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SYSTEM_PROMPT = (
    "Du bist ein Daten-Extraktions-Assistent. "
    "Extrahiere alle Gerichte aus dem Menütext. "
    "Antworte NUR mit einem gültigen JSON-Array. Kein Markdown, keine Erklärungen. "
    'Format: [{"name": "...", "description": "...", "price": 0.0}] '
    "price als Dezimalzahl (0 wenn nicht angegeben). "
    "description leer lassen wenn keine Beschreibung vorhanden."
)

USER_PROMPT_TMPL = (
    "Extrahiere alle Gerichte aus diesem Menütext und gib ein JSON-Array zurück.\n\n"
    "Menütext:\n{text}"
)


def _fetch(url: str) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 200:
            return resp
        log.info("HTTP %s for %s", resp.status_code, url)
        return None
    except Exception as exc:
        log.warning("Fetch error %s: %s", url, exc)
        return None


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav",
                     "aside", "form", "iframe", "svg", "img"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def _is_menu_page(text: str) -> bool:
    lower = text.lower()
    hits = sum(1 for kw in MENU_KEYWORDS if kw in lower)
    return len(text) >= MIN_TEXT_LEN and hits >= 1


def _extract_pdf_text(pdf_bytes: bytes) -> Optional[str]:
    try:
        lines = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    lines.append(text)
        result = "\n".join(lines).strip()
        return result if len(result) >= MIN_TEXT_LEN else None
    except Exception as exc:
        log.warning("PDF parse error: %s", exc)
        return None


def _find_pdf_links(html: str, base_url: str) -> list[str]:
    """Return absolute URLs of PDF links that look like menu documents."""
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        href_lower = href.lower()
        link_text = (a.get_text() or "").lower()
        is_pdf = ".pdf" in href_lower
        is_menu_related = any(kw in href_lower or kw in link_text for kw in MENU_KEYWORDS)
        if is_pdf or (is_menu_related and ".pdf" in href_lower):
            full = urljoin(base_url, href)
            if full not in found:
                found.append(full)
    return found


def _find_menu_link(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        text = (a.get_text() or "").lower()
        if ".pdf" in href:
            continue  # handled separately
        if any(kw in href or kw in text for kw in MENU_KEYWORDS):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == urlparse(base_url).netloc:
                return full
    return None


def find_menu_page(website: str) -> Optional[str]:
    """
    Return menu text from the best source found, or None.
    Priority: PDF links > HTML menu page > path suffixes > homepage fallback.
    """
    base = website.rstrip("/")
    homepage = _fetch(base)
    if homepage is None:
        return None

    # 1. Look for PDF links on the homepage
    pdf_links = _find_pdf_links(homepage.text, base)
    for pdf_url in pdf_links:
        resp = _fetch(pdf_url)
        if resp and resp.content:
            text = _extract_pdf_text(resp.content)
            if text:
                log.info("PDF menu found: %s", pdf_url)
                return f"[PDF: {pdf_url}]\n{text}"
        time.sleep(0.5)

    # 2. Follow HTML menu links on homepage
    menu_link = _find_menu_link(homepage.text, base)
    if menu_link and menu_link != base:
        resp = _fetch(menu_link)
        if resp:
            # Check for PDFs on the linked menu page too
            pdf_links2 = _find_pdf_links(resp.text, menu_link)
            for pdf_url in pdf_links2:
                resp2 = _fetch(pdf_url)
                if resp2 and resp2.content:
                    text = _extract_pdf_text(resp2.content)
                    if text:
                        log.info("PDF on menu page: %s", pdf_url)
                        return f"[PDF: {pdf_url}]\n{text}"
                time.sleep(0.5)
            text = _extract_text(resp.text)
            if _is_menu_page(text):
                log.info("HTML menu link: %s", menu_link)
                return text

    # 3. Try common path suffixes
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    for path in MENU_PATHS:
        url = origin + path
        if url == base:
            continue
        resp = _fetch(url)
        if resp:
            pdf_links3 = _find_pdf_links(resp.text, url)
            for pdf_url in pdf_links3:
                resp2 = _fetch(pdf_url)
                if resp2 and resp2.content:
                    text = _extract_pdf_text(resp2.content)
                    if text:
                        log.info("PDF at path %s: %s", path, pdf_url)
                        return f"[PDF: {pdf_url}]\n{text}"
                time.sleep(0.5)
            text = _extract_text(resp.text)
            if _is_menu_page(text):
                log.info("HTML menu path: %s", url)
                return text
        time.sleep(0.5)

    # 4. Fall back to homepage text
    text = _extract_text(homepage.text)
    if _is_menu_page(text):
        return text

    return None


def parse_dishes(client: anthropic.Anthropic, menu_text: str) -> Optional[list[dict]]:
    truncated = menu_text[:MAX_TEXT_LEN]
    raw = ""
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": USER_PROMPT_TMPL.format(text=truncated)}],
        )
        if msg.stop_reason == "max_tokens":
            log.warning("Response truncated by max_tokens, attempting partial parse")
        raw = next((b.text for b in msg.content if b.type == "text"), "")
        clean = re.sub(r"^```[a-z]*\n?", "", raw.strip())
        clean = re.sub(r"\n?```$", "", clean).strip()
        # If JSON was cut off mid-stream, try to recover by closing the array
        if not clean.endswith("]"):
            last_brace = clean.rfind("}")
            if last_brace != -1:
                clean = clean[:last_brace + 1] + "\n]"
        dishes = json.loads(clean)
        if isinstance(dishes, list):
            return dishes
        log.error("Unexpected Claude response shape: %s", type(dishes))
        return None
    except Exception as exc:
        log.error("Claude parse error: %s | raw=%s", exc, raw[:300])
        return None


def needs_scraping(r: dict) -> bool:
    return bool(r.get("website")) and not r.get("wolt_menu") and not r.get("site_menu")


# ---------------------------------------------------------------------------
# Playwright fallback — only used for sites that returned empty via requests
# ---------------------------------------------------------------------------

PLAYWRIGHT_PAUSE = 3.0


def _find_menu_page_playwright(website: str) -> Optional[str]:
    """
    Try to find a menu page using Playwright (handles JS-rendered sites).
    Returns clean body text, or None if nothing useful found.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth

    base = website.rstrip("/")
    parsed = urlparse(base)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    candidates = [base] + [origin + p for p in MENU_PATHS]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="de-DE",
            ignore_https_errors=True,
        )
        page = ctx.new_page()
        Stealth().use_sync(page)

        try:
            for url in candidates:
                try:
                    page.goto(url, timeout=15_000)
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    log.warning("Playwright timeout: %s", url)
                    continue

                # Dismiss common cookie consent buttons
                for selector in [
                    "button:has-text('Akzeptieren')",
                    "button:has-text('Alle akzeptieren')",
                    "button:has-text('Accept')",
                    "button:has-text('Zustimmen')",
                    "[id*='accept']",
                    "[class*='accept']",
                ]:
                    try:
                        btn = page.locator(selector).first
                        if btn.is_visible(timeout=1000):
                            btn.click()
                            page.wait_for_load_state("networkidle", timeout=5_000)
                            break
                    except Exception:
                        pass

                # Also look for PDF links on the loaded page
                html = page.content()
                pdf_links = _find_pdf_links(html, url)
                for pdf_url in pdf_links:
                    resp = _fetch(pdf_url)
                    if resp and resp.content:
                        pdf_text = _extract_pdf_text(resp.content)
                        if pdf_text:
                            log.info("Playwright→PDF: %s", pdf_url)
                            return f"[PDF: {pdf_url}]\n{pdf_text}"

                text = page.inner_text("body").strip()
                # Collapse whitespace runs
                text = re.sub(r"\n{3,}", "\n\n", text)
                if _is_menu_page(text):
                    log.info("Playwright found menu: %s (%d chars)", url, len(text))
                    return text

                if url != base:
                    time.sleep(0.5)

        finally:
            browser.close()

    return None


def retry_with_playwright() -> None:
    """
    Re-process restaurants where site_menu==[] using Playwright.
    These are sites that requests couldn't render (JS-heavy).
    """
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    targets = [r for r in restaurants if r.get("site_menu") == [] and r.get("website")]

    print(f"Playwright retry: {len(targets)} restaurants with empty site_menu")
    if not targets:
        print("Nothing to retry.")
        return

    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE}\n")

    client = anthropic.Anthropic()
    ok = skipped = errors = 0
    n = len(targets)

    for restaurant in restaurants:
        if restaurant.get("site_menu") != [] or not restaurant.get("website"):
            continue

        name = restaurant["name"]
        website = restaurant["website"]
        num = ok + skipped + errors + 1
        print(f"[{num}/{n}] {name}  (Playwright)")
        print(f"  {website}")

        try:
            menu_text = _find_menu_page_playwright(website)
        except Exception as exc:
            log.error("Playwright error %s: %s", name, exc)
            print(f"  → Playwright error: {exc}")
            errors += 1
            time.sleep(PLAYWRIGHT_PAUSE)
            continue

        if not menu_text:
            print("  → still no menu found")
            skipped += 1
            time.sleep(PLAYWRIGHT_PAUSE)
            continue

        source = "PDF" if menu_text.startswith("[PDF:") else "HTML/JS"
        print(f"  → {source}: {len(menu_text)} chars — asking Claude…")
        dishes = parse_dishes(client, menu_text)

        if dishes is None:
            print("  → Claude parse failed")
            log.error("Playwright dishes parse failed: %s", name)
            errors += 1
        elif not dishes:
            print("  → 0 dishes returned")
            skipped += 1
        else:
            restaurant["site_menu"] = dishes
            print(f"  → {len(dishes)} dishes saved")
            ok += 1

        DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
        time.sleep(PLAYWRIGHT_PAUSE)

    print(f"\nPlaywright done. Extracted: {ok}  Skipped: {skipped}  Errors: {errors}")


def main() -> None:
    restaurants: list[dict] = json.loads(DATA_FILE.read_text())
    targets = [r for r in restaurants if needs_scraping(r)]

    print(f"Loaded {len(restaurants)} restaurants")
    print(f"Targets (website, no menu): {len(targets)}")

    if not targets:
        print("Nothing to scrape.")
        return

    shutil.copy2(DATA_FILE, BACKUP_FILE)
    print(f"Backup → {BACKUP_FILE}\n")

    client = anthropic.Anthropic()
    ok = skipped = errors = 0
    n = len(targets)

    for restaurant in restaurants:
        if not needs_scraping(restaurant):
            continue

        name = restaurant["name"]
        website = restaurant["website"]
        num = ok + skipped + errors + 1
        print(f"[{num}/{n}] {name}")
        print(f"  {website}")

        menu_text = find_menu_page(website)
        if not menu_text:
            print("  → no menu page found")
            log.info("No menu page: %s (%s)", name, website)
            restaurant["site_menu"] = []
            skipped += 1
            DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
            time.sleep(PAUSE)
            continue

        print(f"  → {len(menu_text)} chars — asking Claude…")
        dishes = parse_dishes(client, menu_text)

        if dishes is None:
            print("  → Claude parse failed")
            log.error("Dishes parse failed: %s", name)
            errors += 1
        elif not dishes:
            print("  → 0 dishes returned")
            restaurant["site_menu"] = []
            skipped += 1
        else:
            restaurant["site_menu"] = dishes
            print(f"  → {len(dishes)} dishes saved")
            ok += 1

        DATA_FILE.write_text(json.dumps(restaurants, ensure_ascii=False, indent=2))
        time.sleep(PAUSE)

    print(f"\nDone. Extracted: {ok}  Empty/skipped: {skipped}  Errors: {errors}")


if __name__ == "__main__":
    import sys
    if "--playwright" in sys.argv:
        retry_with_playwright()
    else:
        main()
