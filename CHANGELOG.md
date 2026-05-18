# Changelog

## 2026-05-19

### Данные
- **Kreuzberg North Patch (фаза 4)** — собрано 298 новых ресторанов в bbox 52.490–52.513 (Friedrichshain/северный Кройцберг) через Google Places Nearby Search. Дедупликация против 1430 существующих записей.
- **Wolt scanning north patch** — 115/298 ресторанов найдено на Wolt, 7835 блюд собрано через Playwright.
- **Claude КБЖУ оценка** — 7835/7835 блюд оценено (confidence=medium). Batches API, batch: msgbatch_01R2G8D8NimQ9K1CCdbjiHkb.
- **Dietary flags** — добавлены is_vegan, is_vegetarian, is_gluten_free, is_diabetic_friendly для новых 7835 блюд.
- **Merge** — all_restaurants.json обновлён: 1430 → 1728 ресторанов.

---

## 2026-05-18

### Данные
- **Диетические флаги** — Claude Batches API (haiku-4-5) обогатил 30 710 блюд флагами `is_vegan`, `is_vegetarian`, `is_gluten_free`, `is_diabetic_friendly`, `allergens` (EU-14). 31 батч по 1000 запросов.
- **Фотографии для всех ресторанов** — `fetch_missing_photos.py` получил фото через Google Places API для 262 ресторанов, у которых было меню, но не было фото. До 3 фото на ресторан.
- **Часы работы и сервисы** — `google_places_updater.py` обогатил 409 ресторанов: opening_hours, price_level, dine_in/takeout/delivery.

### Карта
- **Pill-маркеры** — заменили зелёные/серые кружки на белые bubble-баблы в стиле Airbnb с текстом «N dishes». Рестораны не в фильтрах скрыты с карты.
- **Hover-подсветка маркера** — при наведении на карточку ресторана пин на карте становится чёрным.

### Интерфейс
- **Desktop Airbnb-лейаут** — список слева, карта справа (46%) с отступами и скруглёнными углами.
- **Mobile bottom sheet** — карта на весь экран, снизу выезжающий лист со списком ресторанов. Свайп вверх — раскрыть, кнопка «Map» — свернуть.
- **Слайдер фотографий** — стрелки prev/next и точки-индикаторы. Фото слева на десктопе, сверху на мобильном. Стрелки на десктопе появляются только при наведении мыши.
- **Тег Open/Closed** — зелёный/красный тег с часами работы на сегодня в карточке ресторана.
- **Фильтры Gluten-free и Diabetes-friendly** — подключены к реальным данным из диетических флагов.
- **Wolt-лого и кнопка навигации** — в каждой карточке ссылка на Wolt и кнопка-ромб для маршрута в Google Maps.
- **Белый рестайл** — белый фон, карточки #F4F4F4, убраны все тени и рамки.

---

## 2026-05-15

### Данные
- **Граммовка порций** — `weight_estimator.py` добавил `weight_grams_estimate` для 28 048 блюд через Claude Batches API. Тоггл «на 100г» теперь работает для всех блюд.

### Интерфейс
- **AI Advisor** — клик на блюдо открывает модал с описанием, рейтингом питательности и советами. Ответ генерирует claude-haiku-4-5 в реальном времени через Flask-бэкенд.
- **Деплой на Railway** — приложение доступно онлайн. Flask-сервер (`server.py`), GitHub repo: `gkvasnikov/nutrition-app`, автодеплой при push в main.
- **Краткие описания ресторанов** — `fetch_editorial_summary.py` получил editorial summary от Google Places для 154 ресторанов. Показываются курсивом под адресом.
- **Сортировка** — три режима: по соответствию / по расстоянию / A–Z.

---

## 2026-05-14

### Данные
- **Кройцберг** — 875 ресторанов собраны через Google Places Nearby Search. Wolt меню — 266 ресторанов, 17 416 блюд. КБЖУ оценены Claude haiku (confidence=medium).
- **Вранглькиц** — 414 ресторанов, 138 с Wolt-меню (8 765 блюд). КБЖУ оценены Claude haiku.
- **Merge** — `data/all_restaurants.json` = 1 430 ресторанов (Mitte + Kreuzberg + Wrangelkiez, 126 дубликатов удалено).
- **PDF-меню Кройцберг** — 70 PDF скачано, 56 распознаны через pdfplumber + Claude haiku Batches API.
- **OpenFoodFacts заброшен** — систематически завышает калории в ~2× (агрегирует raw ingredients). Все OFF-данные удалены, заменены Claude-оценками.

---

## 2026-05-13

### Данные
- **Google Places** — `google_places_enricher.py` добавил google_place_id, rating, photo_url, phone для 267 ресторанов Mitte.
- **Chains enricher** — официальные КБЖУ для dean&david (181 блюдо) и Nordsee (11 блюд) из PDF и сайта.
- **Starbucks** — 82 позиции: 46 food items из официального PDF (confidence=high) + 36 напитков (claude).
- **Green & Protein** — 57/58 блюд из FoodAmigos API (confidence=high, source=official).
- **kbju_estimator.py --reimprove** — переоценка 1 712 блюд с низким confidence улучшенным промптом.

### Интерфейс
- **Google Maps** — заменили Leaflet+OSM на Google Maps JavaScript API. Фото ресторанов, рейтинг, клик по названию открывает Google Maps карточку в модале.
- **Apple-редизайн** — frosted glass header, segmented controls, pill macros, confidence dots, SVG-пин.
- **Удалены бары и алкоголь** — 606 алкогольных позиций удалено, рестораны без ≥3 food items удалены из БД.

---

## 2026-05-12

### Данные
- **OSM Overpass** — 268 ресторанов Berlin Mitte с координатами и метаданными.
- **Wolt меню (Playwright)** — 66/73 ресторанов, 3 872 блюда.
- **Lieferando меню (Playwright + stealth)** — 156 ресторанов, 16 заменили Wolt (Lieferando богаче).
- **Site scraper** — 58 ресторанов с меню, 3 402 блюда (requests + BeautifulSoup + pdfplumber + Claude haiku).
- **Claude КБЖУ** — 4 258/4 260 блюд оценены через Batches API (~$0.85).
- **Subway** — 102/123 позиций обновлены до confidence=high из официального немецкого PDF.

### Интерфейс
- **MVP фронтенд** — single-page HTML + vanilla JS. 8 пресетов питания, КБЖУ-слайдеры, карта Leaflet+OSM, карточки ресторанов, геолокация, сортировка.
- **Confidence теги** — цветные теги high/medium/low с диапазонами КБЖУ (±10–30%).
- **Тоггл per-100g / per-serving**.
