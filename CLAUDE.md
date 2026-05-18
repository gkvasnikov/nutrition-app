# Restaurant MVP Project
## Цель проекта
Приложение для рекомендации ресторанов по целям питания.
MVP: Berlin Mitte, ~100 ресторанов, простой веб-интерфейс.
## Структура данных
Основной файл: data/restaurants.json
Формат: [{id, name, lat, lon, cuisine, website, 
          address, source, menu: [{name, description,
          calories, protein, fat, carbs, confidence}]}]
## Источники данных
1. OSM Overpass API — координаты и метаданные
2. Wolt API (неофициальный) — меню где есть
3. Lieferando API (неофициальный) — меню где есть  
4. Playwright scraper — парсинг сайтов для остальных
5. Claude API — оценка КБЖУ по описанию блюда
## Bbox Berlin Mitte
52.516, 13.388, 52.526, 13.408
## Технологический стек
Python 3.11+, Playwright, requests, fuzzywuzzy (матчинг)
Фронтенд: простой HTML + vanilla JS, читает restaurants.json
## Важные принципы
- Между запросами к внешним API всегда задержка 1-2 сек
- confidence = high/medium/low для каждой КБЖУ оценки
- Логировать все ошибки парсинга в errors.log
- Не трогать restaurants.json без бэкапа

## Статус
- [x] OSM Overpass fetch — выполнено (2026-05-12). Скачано 268 заведений, сохранено в data/restaurants.json. Скрипт: scrapers/osm_fetch.py
- [ ] Wolt API (официальный) — заблокирован (error 430 на меню без авторизации)
- [x] Wolt меню через Playwright — выполнено (2026-05-12). 73 заведения в bbox, scraped 66/73, 3872 позиций меню (avg 66/ресторан). Поле wolt_menu добавлено в restaurants.json. Скрипт: scrapers/wolt_fetch.py
- [x] Lieferando меню через Playwright + stealth — выполнено (2026-05-12). 156 заведений в 10178, 16 заменили Wolt (Lieferando богаче), 22 оставили Wolt. Скрипт: scrapers/lieferando_fetch.py
- [ ] Playwright scraper — парсинг сайтов для остальных (пропущен в MVP, достаточно Wolt+Lieferando)
- [x] Claude API — оценка КБЖУ — выполнено (2026-05-12). 4258/4260 блюд обогащено. Batches API (claude-haiku-4-5), ~$0.85. Скрипт: enrichment/kbju_estimator.py
- [x] Фронтенд — выполнено (2026-05-12). Single-page HTML + vanilla JS. 8 пресетов питания (заполняют слайдеры), конфигуратор КБЖУ с слайдерами от/до для каждого макронутриента + переключатель веганского + кнопка сброса, карта Leaflet+OSM с зелёными/серыми маркерами, карточки ресторанов с КБЖУ, геолокация, сортировка по кол-ву подходящих блюд + расстоянию. Файл: frontend/index.html. Сервер: python3 -m http.server 8080, открывать /frontend/index.html
- [x] Subway КБЖУ из официального PDF — выполнено (2026-05-12). 102/123 позиций обновлены до confidence=high из "German Nutritional Information Full Menu March 2025". 21 не сматчено (meal deals, catering plates, региональные напитки, Smashed Falafel — нет в PDF). Скрипт: enrichment/subway_update.py
- [x] Confidence теги на блюдах — выполнено (2026-05-12). Фронтенд показывает цветные теги: high=зелёный, medium-high=тёмно-зелёный, medium=жёлтый, low=серый. Диапазон: high — точное значение, medium-high ±10%, medium ±15%, low ±30%.
- [x] Site scraper — выполнено (2026-05-12). scrapers/site_scraper.py: requests+BeautifulSoup, PDF-ссылки (pdfplumber), Claude haiku для извлечения меню (max_tokens=8192), Playwright fallback (--playwright флаг). Результат: 58 ресторанов с site_menu, 3402 блюда. Поле site_menu добавлено в restaurants.json.
- [x] kbju_estimator.py обновлён для site_menu — выполнено (2026-05-12). Обрабатывает wolt_menu и site_menu. Custom ID: r{idx}-i{idx}-w/s. Обогащено 3401/3403 блюд site_menu.
- [x] FatSecret enricher — заброшен (2026-05-12). Free plan только US-датасет, почти нет совпадений с блюдами берлинских ресторанов.
- [x] Chains enricher — выполнено (2026-05-13). enrichment/chains_enricher.py: dean&david 181/230 + Nordsee 11/43. confidence="high", source="official". PDFs кэшируются в enrichment/pdfs/, Nordsee в enrichment/nordsee_nutrition.json. Nordsee: данные per 100g с сайта nordsee.com/de/produkte через Playwright. Wolt-меню Nordsee содержит чужое (döner) меню — большинство не совпало корректно.
- [x] OpenFoodFacts enricher — заброшен (2026-05-14). OFF систематически завышает калории в ~2x (медиана) из-за агрегации raw ingredient-данных без учёта пропорций в блюде. Все OFF-данные удалены из restaurants.json, заменены Claude-оценками. Бэкап до удаления: data/restaurants_backup_before_strip.json.
- [x] Тоггл per-100g / per-serving во фронтенде — выполнено (2026-05-12). Кнопка переключает режим отображения. HIGH: точные цифры. Medium/low: диапазоны. Поля calories_100g/protein_100g/fat_100g/carbs_100g + serving_g существовали только для OFF-данных — после удаления OFF тоггл показывает только per-serving.
- [x] Фронтенд показывает 132 ресторана — 74 wolt + 58 site (объединяет wolt_menu и site_menu).
- [x] Google Places enricher — выполнено (2026-05-13). enrichment/google_places_enricher.py: Find Place by Text + Place Details. 267 ресторанов (1 удалён — BITE by RitterRichard не найден): google_place_id (267/267), rating (264/267), photo_url (264/267), phone (216/267). photo_url — прямые ссылки на Google Places Photos API (maxwidth=800). API ключ в .env (GOOGLE_PLACES_API_KEY). Resume: пропускает уже обработанные (есть google_place_id).
- [x] Фронтенд обновлён до Google Maps — выполнено (2026-05-13). Leaflet+OSM заменён на Google Maps JavaScript API. Карточки: фото (photo_url), рейтинг рядом с названием, клик по названию открывает Google Maps Embed API modal (полная карточка заведения). Маркеры: SVG круги (зелёные=совпадают, серые=нет). Клик по маркеру → скролл к карточке. Клик по названию → анимированный зум, желтая подсветка маркера, Place modal.
- [x] Удалены бары без еды и алкогольные напитки — выполнено (2026-05-13). 606 алкогольных позиций удалено из menus. Рестораны без ≥3 food items с калориями — удалены из БД. Бэкап: data/restaurants_backup_clean.json.
- [x] Starbucks меню обновлено — выполнено (2026-05-13). 82 позиции: 46 food items (confidence=high, source=official, из официального PDF) + 36 drinks (confidence=medium, source=claude). Оба Starbucks в БД обновлены. menu_source="starbucks_official". Скрипт: /tmp/sbux_kbju.py (использовал прямые данные, без Batches API).
- [x] kbju_estimator.py --reimprove — выполнено (2026-05-13). Новый режим: улучшенный промпт для source='claude'/source='' + confidence=low/medium. 1712/1713 блюд переоценено. Добавлены поля weight_grams_estimate и reasoning. PROTECTED_SOURCES не перезаписываются. Batches API (claude-haiku-4-5). Отдельный batch_id файл: kbju_reimprove_batch_id.txt.
- [x] Редизайн фронтенда в стиле Apple — выполнено (2026-05-13). Palette #f5f5f7, frosted glass header, segmented controls, pill macros, confidence dots вместо текстовых тегов, SVG-пин вместо emoji, card-body wrapper для чистого фото bleed. Убраны все emoji из UI.
- [x] Green & Protein официальные КБЖУ — выполнено (2026-05-13). 57/58 блюд обновлено до source=official, confidence=high. Данные из FoodAmigos API (order.greenandprotein.de). Per-100g + serving_g по категории (Bowls 400г, Salads 350г, Wraps 300г, Smoothies 270г, Burgers 280г, Dressings 30г, Shots 60г). Скрипт: /tmp/gp_update.py.
- [x] Сортировка в фронтенде — выполнено (2026-05-15). Три режима: "По соответствию" (по кол-ву подходящих блюд, затем по расстоянию), "По отдалённости" (геодистанция), "A–Z" (алфавит). Кнопки-таблетки под счётчиком результатов.
- [x] Граммовка порций (weight_grams_estimate) — выполнено (2026-05-15). enrichment/weight_estimator.py: Claude Batches API (claude-haiku-4-5, max_tokens=60), добавляет weight_grams_estimate ко всем блюдам. Запущен на all_restaurants.json: 28048/28050 блюд. Тоггл "на 100г" теперь работает для всех блюд. В режиме "на порцию" показывает ~Xг.
- [x] Карточка блюда с AI Advisor — выполнено (2026-05-15). Клик на любое блюдо открывает модал: фото ресторана, название, КБЖУ, блок AI Advisor. Advisor подгружается в момент открытия (claude-haiku-4-5, ~1-2 сек): что за блюдо (краткое описание), рейтинг (Ограниченно/Нормально/Хорошо/Питательно) со шкалой, советы по нутриентам. Требует Flask-бэкенд: server.py.
- [x] Editorial summary ресторанов — выполнено (2026-05-15). enrichment/fetch_editorial_summary.py: Google Places Details API (поле editorial_summary, language=ru — но Google возвращает только английские тексты). 154/495 ресторанов получили описание. Показывается курсивом под адресом в карточке ресторана. Бэкап: data/all_restaurants_backup_summary.json.
- [x] Деплой на Railway — выполнено (2026-05-15). GitHub repo: gkvasnikov/nutrition-app. Flask-сервер (server.py + Procfile). Env vars: ANTHROPIC_API_KEY, GOOGLE_MAPS_API_KEY. Google Maps API ключ вынесен из HTML в /api/config endpoint. UI переведён на английский язык.
- [x] Bottom sheet layout (мобильный) — выполнено (2026-05-18). Airbnb-стиль: карта на всю высоту экрана, поверх — bottom sheet. Peek-режим (110px): drag pill + счётчик ресторанов. Expand: свайп вверх или тап по handle → лист перекрывает карту, появляется плавающая кнопка "Map". Тап по кнопке или карте → возврат в peek. Desktop-лейаут не изменён.
- [x] Фильтры Gluten-free / Diabetes-friendly — добавлены как заглушки (2026-05-18). Два чекбокса под "Plant-based only" с тегом "soon", disabled. Фильтрация не подключена — нужны данные в меню (поля gluten_free, glycemic_index или эвристика по ингредиентам).
- [x] Google Places обогащение ресторанов (opening_hours, services) — выполнено (2026-05-18). enrichment/google_places_updater.py: Place Details API для 1430 ресторанов. 409 обогащено: opening_hours (244), price_level, dine_in/takeout/delivery, serves_vegetarian_food/breakfast/lunch/dinner, photos (369). Resume: пропускает уже имеющие opening_hours.
- [x] Тег Open now / Closed + часы на карточке ресторана — выполнено (2026-05-18). Функция getOpenStatus() определяет статус по periods (Berlin timezone). Зелёный тег "Open" или красный "Closed" + текст часов сегодня. Формат: (jsDay + 6) % 7 переводит JS (0=Sun) в индекс weekday_text (0=Mon).
- [x] Диетические флаги для блюд — выполнено (2026-05-18). enrichment/dietary_flags_estimator.py: Claude Batches API (claude-haiku-4-5), 31 батч по 1000. 30,710/30,730 блюд: is_vegan (33%), is_vegetarian (57%), is_gluten_free (27%), is_diabetic_friendly (38%), allergens (EU-14). Resume через dietary_flags_batch_ids.txt. Упал на батче 25 (MessageBatchExpiredResult) — возобновлён и завершён.
- [x] Gluten-free и Diabetes-friendly фильтры активированы — выполнено (2026-05-18). Чекбоксы подключены к applyFilter(). Фильтрация по is_gluten_free/is_diabetic_friendly полям из данных. Тег "soon" убран.
- [x] Desktop Airbnb-лейаут — выполнено (2026-05-18). Карта справа во фрейме с border-radius 14px, список слева. CSS flexbox order: list=1, map=2. Ширина карты 46%, список занимает остаток. Мобильный лейаут не затронут.
- [x] Визуальный рестайл (белый фон, карточки #F4F4F4) — выполнено (2026-05-18). --bg: #ffffff, --surface: #F4F4F4. Убраны все box-shadow и border у карточек, конфигуратора, meal tabs, sort row separator. Внутри карточки блюд — белый фон.
- [x] Граммовка блюда рядом с названием в модале — выполнено (2026-05-18). dish-modal-weight span в заголовке модала. Стиль: font-size 0.75rem, muted цвет, margin-left 8px.
- [x] Сортировка по умолчанию — Nearest (2026-05-18). sortMode = 'dist'. Кнопка Low confidence убрана (confLow жёстко false в getFilters).
- [x] Радиус по умолчанию 500м — выполнено (2026-05-18). Слайдер value="500".
- [x] Центровка карты по геолокации при загрузке + радиус-круг — выполнено (2026-05-18). getLocation() при старте panTo на координаты пользователя, показывает #dist-row и radius circle сразу.
- [x] Мобильный UX улучшения — выполнено (2026-05-18). Статус-строка и фильтры на разных строках. Блюда в вертикальный стек (dish-row flex-direction:column). Модал с 16px отступами по бокам (calc(100vw - 32px)). sheetPeek() вызывается при клике на маркер карты и на кнопку "Map".
- [x] Слайдер thumb 24px — выполнено (2026-05-18). ::-webkit-slider-thumb и ::-moz-range-thumb width/height: 24px.

## Заметки
- На машине Python 3.9 (не 3.11+), синтаксис `X | None` не работает — используем `Optional[X]` из typing + `from __future__ import annotations`
- OSM вернул 268 элементов из 268 — все именованные (безымянные пропускаются)
- way/relation элементы используют поле `center` для координат
- Wolt v3/venues/slug/{slug} и v3/venues/slug/{slug}/menu возвращают error 430 без токена — используем Playwright для wolt.com
- Lieferando/Takeaway cw-api.takeaway.com блокирует Cloudflare (429 → 403); lieferando.de отдаёт 403 на все запросы — пропущен
- Fuzzy-матчинг: fuzz.token_set_ratio по имени + частичное сравнение адреса, порог 65. 7 из 73 Wolt-заведений не нашли пару в OSM (новые заведения или ghost kitchens)
- playwright-stealth (Stealth().use_sync) обходит Cloudflare на lieferando.de; импорт: `from playwright_stealth import Stealth`
- Lieferando меню грузится lazy-load по scroll; нужно жать End в цикле до стабилизации count [data-item-id]
- menu_source поле: "wolt" (явно) или "lieferando" (явно); если поля нет — данные от Wolt (старые записи)
- weight в wolt_menu часто пустой — Wolt не показывает граммаж в карточках, только в описании если упомянут явно
- Claude Batches API: claude-haiku-4-5 оборачивает ответ в ```json ... ``` — нужно strip кода перед json.loads(); используем re.sub
- kbju_estimator.py сохраняет batch_id в enrichment/kbju_batch_id.txt для resume при прерывании; файл удаляется после успешного завершения
- Фронтенд запускается через: cd "Nutrition App" && python3 server.py, затем http://localhost:8080/frontend/index.html (Flask, не http.server — нужен для /api/advice)
- Веганский фильтр в фронтенде — эвристика по ключевым словам в названии/описании блюда; неточный, но рабочий для MVP
- Subway PDF: pdfplumber + column mapping [0]=name, [1]=вес(г), [3]=kcal, [4]=жиры, [6]=углеводы, [9]=белки. 90 блюд в PDF (Sandwiches 15/30cm, Wraps, Salads, Panini, Kids Meals, Desserts, Drinks). Бэкап: data/restaurants_backup_subway.json
- OpenFoodFacts API: часто возвращает 503 — добавлен retry (2 попытки, 5s/10s задержка), pause=1.0s. Важно: enrich_from_off() должна пропускать уже обогащённые items (source in PROTECTED_SOURCES), иначе при двух параллельных процессах возникает race condition с перезаписью restaurants.json.
- OFf enricher хранит batch_id в enrichment/off_batch_id.txt для resume; файл удаляется после сбора ингредиентов (до Phase 2).
- site_scraper.py: --playwright флаг для Playwright fallback; Claude haiku max_tokens=8192 (меньше — truncated JSON); fallback на усечённый JSON: находим последний `}` и дописываем `\n]`.
- FatSecret: Client ID/Secret в env vars FATSECRET_CLIENT_ID/FATSECRET_CLIENT_SECRET. Free plan = только US. Whitelisting IP на platform.fatsecret.com.
- Google Places API: Find Place by Text не поддерживает поля formatted_phone_number и website — вернёт INVALID_REQUEST. Эти поля только через Place Details. В find_place() использовать только: place_id,name,rating,user_ratings_total,price_level,opening_hours,photos.
- Google Places enricher: при запуске параллельно с OFF enricher (который тоже пишет в restaurants.json) возникает race condition — данные перезаписываются. Запускать последовательно.
- OpenFoodFacts vs Claude точность: сравнение 1193 пар показало OFF выше Claude в 92% случаев, медиана 2.06x, среднее 2.71x. Причина: OFF агрегирует nutrition per-100g для raw ингредиентов без учёта их доли в блюде. Claude-оценки реалистичнее для ресторанных блюд.
- kbju_estimator.py --force-confidence <high|medium|low> — переопределяет confidence для всех оцениваемых блюд. Использовали medium для замены OFF-данных и первичной оценки Kreuzberg.
- PROTECTED_SOURCES в kbju_estimator.py: убран 'openfoodfacts' — теперь {'official', 'fatsecret'}. OFF-данные больше не защищены.
- Starbucks.de блокирует после ~15 запросов (HTTP 429). Next.js `self.__next_f.push` данные доступны до блокировки. При блокировке — использовать официальный PDF или оценку Claude.
- Google Maps Embed API modal: `https://www.google.com/maps/embed/v1/place?key=KEY&q=place_id:PLACE_ID&language=de` — показывает полную карточку заведения. Требует тот же API ключ что и Maps JavaScript API.
- Фронтенд: Leaflet удалён, Google Maps JavaScript API подключается async через callback=onGMapsLoad. Координация: gmapsReady + dataReady флаги, startApp() вызывается когда оба true.
- kbju_estimator.py --reimprove: фильтрует source='claude' ИЛИ source='' (старые оценки без source) + confidence in ('low','medium'). PROTECTED_SOURCES = {'official','openfoodfacts','fatsecret'} никогда не перезаписываются. ANTHROPIC_API_KEY загружается через dotenv из .env.
- Green & Protein использует FoodAmigos API: https://app.foodamigos.io/api/companies/1653/menus — возвращает nutrition_per_100 (energy_kcal, protein_g, fat_g, carbohydrate_g) для каждого продукта. Числа могут быть строками с запятой вместо точки ('0,5') — нужен to_f() с заменой.
- server.py (Flask): отдаёт статику frontend/ и data/, плюс POST /api/advice → claude-haiku-4-5 → {what, rating, advice}. load_dotenv нужен с override=True иначе ANTHROPIC_API_KEY не подхватывается. Зависимость: pip3 install flask.
- editorial_summary: Google Places возвращает его только для ~30% заведений (популярные места). Параметр language=ru игнорируется — тексты всегда на английском.
- weight_estimator.py: запускать на конкретном файле через --file, или без аргументов на all_restaurants.json. Batch ID хранится в enrichment/weight_{stem}_batch_id.txt, удаляется после завершения.
- Bottom sheet: SHEET_PEEK_H = 110px. Состояния управляются через CSS-класс sheet-expanded на #list-panel. sheetExpand() / sheetPeek() — публичные JS-функции. Drag через touch events на #sheet-handle. #sheet-scroll (overflow-y: hidden в peek, auto в expanded).
- Google Maps API ключ: Application restrictions = None (не ставить Website restrictions — ломает загрузку фото через <img> тег, т.к. браузер не всегда отправляет Referer для cross-origin изображений).
- Railway: деплой автоматически при push в main. PORT выставляется Railway автоматически, server.py читает через os.environ.get("PORT", 8080).
- dietary_flags_estimator.py: custom_id формат r{r_idx}-{src}-i{i_idx} (src=w/s). dietary_flags_batch_ids.txt хранит все 31 ID, по одному в строке. Resume: пропускает блюда где уже есть is_vegan. is_diabetic_friendly=null если item.confidence=='low'.
- MessageBatchExpiredResult не имеет атрибута .error — использовать getattr(result.result, "error", result.result.type) для обработки всех non-succeeded типов (errored, expired, canceled).
- Ворктри-workflow: правки идут в worktree, но preview-сервер отдаёт файлы из основного проекта (/Users/george/Claude/Nutrition App/). Перед проверкой: cp worktree/frontend/index.html main/frontend/index.html.
- getOpenStatus: weekday_text[idx] где idx = (jsDay + 6) % 7 переводит JS-день (0=Sun) в немецкий порядок (0=Mon). periods.open/close.day тоже 0=Sun — для ночных заведений (close.day != open.day) проверяем оба дня отдельно.
- google_places_updater.py: _find_env() поднимается на 6 уровней вверх от ROOT — нужно т.к. ворктри вложен глубоко. Atomic save через .tmp → replace.

## Инструкция для Claude
После выполнения каждой задачи:
1. Обнови раздел ## Статус
2. Если появились новые важные детали 
   (формат API изменился, найден баг) — 
   добавь в ## Заметки
3. Не удаляй старые записи, только добавляй


## Режим работы
Этот проект выполняет автоматический парсинг.
Не запрашивай подтверждение на каждый HTTP запрос,
сетевую операцию или запись в файл.
Выполняй все операции автоматически.
4. Claude API КБЖУ — ✓ (4034/7058 блюд обогащено, Batches API)

## Кройцберг (фаза 2)
### Данные
- Директория: /data/kreuzberg/
- restaurants_kreuzberg.json — основная БД (формат идентичен restaurants.json + поля lieferando_menu, kbju_status)
- verified_wolt.json — подтверждённые Wolt slug'и
- verified_lieferando.json — подтверждённые Lieferando URL'ы
- sites_with_kbju.json — сайты с потенциальными КБЖУ для парсинга

### Скрипты
- scrapers/kreuzberg/google_places_collector.py — сбор ресторанов через Google Places Nearby Search
- scrapers/kreuzberg/wolt_scanner.py — меню через Playwright (wolt.com)
- scrapers/kreuzberg/lieferando_scanner.py — меню через Playwright + stealth (lieferando.de)
- scrapers/kreuzberg/site_detector.py — проверка сайтов на наличие КБЖУ/PDF меню
- scrapers/kreuzberg/site_parser.py — парсинг сайтов + Claude haiku для структурирования
- scrapers/kreuzberg/chains_scraper.py — копирует официальные данные сетей из Mitte БД

### Bbox Кройцберга
52.4878, 13.3800, 52.5100, 13.4300

### Статусы kbju_status
- verified_wolt — меню получено с Wolt (Playwright)
- verified_lieferando — меню получено с Lieferando (Playwright + stealth)
- verified_site — меню распарсено с официального сайта
- verified_pdf — КБЖУ из официального PDF (сети: Subway, Starbucks и др.)
- estimated — оценка Claude haiku (source=claude)
- no_data — меню не найдено

### Порядок запуска
1. google_places_collector.py — заполняет restaurants_kreuzberg.json
2. chains_scraper.py — быстрое обогащение сетевых ресторанов
3. wolt_scanner.py — Wolt меню
4. lieferando_scanner.py — Lieferando меню
5. site_detector.py → site_parser.py — сайты с КБЖУ
6. kbju_estimator.py (из корня) — Claude оценка для оставшихся

### Статус задач
- [x] Google Places Nearby Search сбор — выполнено. 875 ресторанов в data/kreuzberg/restaurants_kreuzberg.json
- [x] Wolt scanning — выполнено. 266 ресторанов с wolt_menu (без КБЖУ — Wolt не публикует нутриенты)
- [ ] Lieferando scanning — не запущен
- [x] Site detection + parsing — выполнено. 578 сайтов проверено, 80 с КБЖУ сигналами, 4 распаршено успешно
- [x] Claude КБЖУ оценка — выполнено (2026-05-14). 17416/17419 блюд, confidence=medium, source=claude. Batch: msgbatch_01YXyMgy8cGAdPAFGQcZ9RqR. Скрипт: python3 enrichment/kbju_estimator.py --file data/kreuzberg/restaurants_kreuzberg.json --force-confidence medium
- [x] Merge districts — выполнено. data/all_restaurants.json = 1430 ресторанов (267 Mitte + 875 Kreuzberg + 414 Wrangelkiez - 126 дубликатов). Скрипт: data/merge_districts.py
- [x] Фронтенд обновлён — показывает all_restaurants.json, центр карты 52.510,13.400
- [x] PDF меню скачаны — выполнено (2026-05-14). 70 PDF в data/kreuzberg/menus/. Источник: sites_with_kbju_list.txt
- [x] PDF меню распознаны в текст — выполнено (2026-05-14). 56 ресторанов с текстом через pdfplumber + Claude haiku Batches API (msgbatch_01AK1Kqhqj7raPzCzmvPr6pp). 14 — image-based PDFs (плейсхолдеры с пометкой OCR required). Формат: Название | Цена | Аллергены, вариации разбиты на отдельные позиции. Файлы: data/kreuzberg/menu_texts/. Скрипт: enrichment/menu_text_parser.py

### Заметки
- Uber Eats заблокирован: bot detection (setRobotEventsV1), возвращает USD+пустой feedItems. Скрипт написан но не работает: scrapers/kreuzberg/ubereats_scanner.py
- kbju_estimator.py теперь поддерживает --file аргумент для работы с разными районами
- Batch ID файл для Kreuzberg: enrichment/kbju_kreuzberg_batch_id.txt (удаляется после завершения)

## Вранглькиц (фаза 3)
### Данные
- Директория: data/wrangelkiez/
- restaurants_wrangelkiez.json — основная БД (414 ресторанов)
- verified_wolt.json — подтверждённые Wolt slugи
- .wolt_venues_cache.json — кэш Wolt API (2416 заведений, не удалять)

### Bbox
52.490, 13.424, 52.505, 13.460

### Скрипты
- scrapers/wrangelkiez/google_places_collector.py — сбор ресторанов + photo_url
- scrapers/wrangelkiez/wolt_scanner.py — меню через Playwright

### Статус задач
- [x] Google Places сбор — выполнено (2026-05-14). 414 ресторанов с photo_url. Скрипт: scrapers/wrangelkiez/google_places_collector.py
- [x] Wolt scanning — выполнено (2026-05-14). 138/414 ресторанов с меню, 8765 блюд. Скрипт: scrapers/wrangelkiez/wolt_scanner.py
- [x] Claude КБЖУ оценка — выполнено (2026-05-14). 8764/8765 блюд, confidence=medium, source=claude. Скрипт: python3 enrichment/kbju_estimator.py --file data/wrangelkiez/restaurants_wrangelkiez.json --force-confidence medium
- [x] Merge districts — обновлено. all_restaurants.json = 1430 ресторанов

### Заметки
- Wolt location формат: массив [lon, lat], не словарь — учесть при адаптации скриптов для других районов
- Аллергены на Wolt: модал при клике на карточку блюда, но разметка закрытая — попытка не удалась, убрано из скрапера
- 126 дубликатов при merge (Wrangelkiez пересекается с Kreuzberg bbox по восточной части)
