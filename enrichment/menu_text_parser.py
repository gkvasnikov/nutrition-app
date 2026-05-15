"""
menu_text_parser.py
Читает PDF из data/kreuzberg/menus/, извлекает текст через pdfplumber,
парсит через Claude Batches API (haiku), сохраняет .txt в data/kreuzberg/menu_texts/
"""

from __future__ import annotations
import os, json, re, time, sys
import pdfplumber
import anthropic
from dotenv import load_dotenv

load_dotenv()

MENUS_DIR   = "data/kreuzberg/menus"
OUTPUT_DIR  = "data/kreuzberg/menu_texts"
BATCH_ID_FILE = "enrichment/menu_parser_batch_id.txt"
MODEL = "claude-haiku-4-5-20251001"

# Маппинг номер→название ресторана из списка
RESTAURANT_NAMES = {
    "01": "Schmelzwerk in den Sarottihöfen",
    "02": "King King X-Berg",
    "04": "Cafe Milagro",
    "05": "7 Sisters Sushi & more",
    "06": "Little Tibet Restaurant",
    "07": "Fräulein Nimmersatt",
    "08": "El Chilenito",
    "09": "Mido Restaurant Kreuzberg",
    "10": "Mokja Restaurant",
    "11": "AnCom Kitchen",
    "12": "Beumer & Lutum",
    "13": "Seerose",
    "14": "Cafe Brick",
    "15": "tulus lotrek",
    "16": "Wirtshaus zum Mitterhofer",
    "17": "Monti Pizza",
    "18": "Patakha",
    "19": "ammAmma",
    "20": "Lezzet Dünyası",
    "21": "Zeytin Café",
    "23": "Taka Fish House",
    "24": "Café Bethesda",
    "25": "Tischendorf",
    "26": "Gazzo",
    "27": "Anh Ba Restaurant",
    "28": "Avatar Indisches Restaurant",
    "29": "Gran Casino",
    "31": "Amici Amici",
    "32": "Kreuzberger Himmel",
    "33": "RokuHachi Berlin",
    "34": "Pizza Slice",
    "35": "Restaurant Split",
    "37": "Burgermeister",
    "38": "Rutz Zollhaus",
    "39": "Van Loon Restaurant",
    "40": "Cocolo Ramen X-berg",
    "41": "Via He Hai",
    "42": "Taverna To Koutouki",
    "43": "Kreuzberger Weltlaterne",
    "44": "Taverna Athene",
    "45": "Willy's Bistro",
    "46": "Pão de queijo",
    "47": "LAZZAT Café",
    "48": "Golda delux",
    "49": "Cafe Morgenstern",
    "52": "Hasir Burger",
    "53": "Restaurant Panther",
    "54": "Nineteen",
    "55": "Hanuman Thai Curry House",
    "56": "Caphe Hoa 2",
    "57": "Soi189 Kreuzberg",
    "58": "Umami Mitte",
    "60": "Standard Serious Pizza",
    "62": "Fukagawa Ramen XBerg",
    "63": "Kombrink",
    "64": "Alt Berliner Wirtshaus Henne",
    "65": "Chez Michel",
    "66": "Blumental",
    "67": "Mawal-Berlin (Falafelwerk)",
    "68": "PHO Noodlebar Kreuzberg",
    "69": "Beba",
    "70": "Restaurant Tim Raue",
    "71": "Ristorante Lungomare",
    "72": "Little Green Rabbit",
    "73": "Caramel",
    "74": "Viet Checkpoint",
    "75": "60 seconds to napoli",
    "76": "Huong Lua",
    "79": "World of Pizza Berlin-Mitte",
    "80": "Bistro im DAZ",
}

SYSTEM_PROMPT = """Du bist ein Experte für die Analyse von Restaurantmenüs.
Deine Aufgabe: Extrahiere alle Gerichte aus dem Menütext und gib sie strukturiert aus.

REGELN:
1. Format pro Zeile: GERICHTNAME | PREIS | ALLERGENE
   - PREIS: nur die Zahl mit €, z.B. "12,50 €". Falls kein Preis: "-"
   - ALLERGENE: Nummern/Buchstaben wie "1, 2, 3" oder Namen wie "Gluten, Laktose". Falls keine: "-"
2. Variationen aufteilen: Wenn ein Gericht Variationen hat (z.B. "mit Huhn, mit Garnelen, mit Tofu"),
   erstelle SEPARATE Zeilen für jede Variation mit dem vollen Namen:
   Pho mit Huhn | 13,00 € | 1, 2
   Pho mit Garnelen | 14,00 € | 1, 6
   Pho mit Tofu | 12,00 € | 1, 9
3. Kategorien als Überschriften: ### KATEGORIENAME (z.B. ### VORSPEISEN)
4. Keine Getränke ohne Nahrungsmittelwert (Wasser, Softdrinks) aufnehmen, AUSSER sie sind explizit Kategorie
5. Kein JSON, keine Erklärungen — nur das formatierte Menü
6. Erste Zeile: # RESTAURANTNAME"""

def extract_pdf_text(pdf_path: str, max_pages: int = 20) -> str:
    """Извлекает текст из PDF, максимум max_pages страниц."""
    text_parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages = pdf.pages[:max_pages]
            for i, page in enumerate(pages):
                t = page.extract_text()
                if t:
                    text_parts.append(f"[Seite {i+1}]\n{t}")
    except Exception as e:
        print(f"  pdfplumber error: {e}")
    return "\n\n".join(text_parts)


def build_requests(pdf_files: list[str]) -> list[dict]:
    """Строит список запросов для Batches API."""
    requests = []
    for fname in pdf_files:
        num = fname.split("_")[0]
        rest_name = RESTAURANT_NAMES.get(num, fname.replace(".pdf", ""))
        pdf_path = os.path.join(MENUS_DIR, fname)

        print(f"  Extracting text: {fname}")
        raw_text = extract_pdf_text(pdf_path)
        if not raw_text.strip():
            print(f"    WARNING: empty text for {fname}")
            raw_text = "(Kein Text extrahierbar)"

        # Обрезаем до ~15k символов чтобы не превысить контекст
        if len(raw_text) > 15000:
            raw_text = raw_text[:15000] + "\n...[gekürzt]"

        user_msg = f"""Restaurant: {rest_name}

MENÜTEXT:
{raw_text}"""

        requests.append({
            "custom_id": f"menu_{num}",
            "params": {
                "model": MODEL,
                "max_tokens": 4096,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}]
            }
        })
    return requests


def submit_batch(requests: list[dict]) -> str:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    print(f"\nSubmitting batch with {len(requests)} requests...")
    batch = client.messages.batches.create(requests=requests)
    batch_id = batch.id
    print(f"Batch ID: {batch_id}")
    with open(BATCH_ID_FILE, "w") as f:
        f.write(batch_id)
    return batch_id


def poll_batch(batch_id: str, interval: int = 30) -> None:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    print(f"Polling batch {batch_id}...")
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(f"  Status: {batch.processing_status} | "
              f"processing={counts.processing} succeeded={counts.succeeded} "
              f"errored={counts.errored}")
        if batch.processing_status == "ended":
            break
        time.sleep(interval)


def collect_results(batch_id: str) -> dict[str, str]:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    results = {}
    for result in client.messages.batches.results(batch_id):
        cid = result.custom_id
        if result.result.type == "succeeded":
            content = result.result.message.content
            text = content[0].text if content else ""
            results[cid] = text
        else:
            print(f"  ERROR for {cid}: {result.result.type}")
            results[cid] = None
    return results


def save_results(results: dict[str, str]) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    saved = 0
    for cid, text in results.items():
        if not text:
            continue
        num = cid.replace("menu_", "")
        rest_name = RESTAURANT_NAMES.get(num, num)
        # Безопасное имя файла
        safe_name = re.sub(r'[^\w\s\-]', '', rest_name).strip().replace(" ", "_")
        out_path = os.path.join(OUTPUT_DIR, f"{num}_{safe_name}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        saved += 1
        print(f"  Saved: {out_path}")
    print(f"\nTotal saved: {saved} files")


def main():
    # Проверяем resume
    if os.path.exists(BATCH_ID_FILE):
        with open(BATCH_ID_FILE) as f:
            batch_id = f.read().strip()
        print(f"Resuming batch: {batch_id}")
    else:
        # Собираем PDF файлы
        pdf_files = sorted([f for f in os.listdir(MENUS_DIR) if f.endswith(".pdf")])
        print(f"Found {len(pdf_files)} PDF files")

        print("\nExtracting text from PDFs...")
        requests = build_requests(pdf_files)

        batch_id = submit_batch(requests)

    poll_batch(batch_id)
    print("\nCollecting results...")
    results = collect_results(batch_id)
    save_results(results)

    # Удаляем batch_id файл после успеха
    if os.path.exists(BATCH_ID_FILE):
        os.remove(BATCH_ID_FILE)
    print("\nDone!")


if __name__ == "__main__":
    main()
