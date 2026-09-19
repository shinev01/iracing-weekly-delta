# iRacing Weekly Tracker

Локальное приложение для импорта официальной истории гонщика по iRacing Customer ID и анализа изменения iRating по настоящим iRacing race week.

Приложение не использует официальный iRacing API/OAuth и не хранит пароль, email или OAuth token.

## Источники

1. `https://irstats.com/driver/{cust_id}/races?page={page}` — обычная публичная server-rendered HTML-история и источник `subsession_id`.
2. `https://iracing6-backend.herokuapp.com/api/sessionData/results/{subsession_id}` — точный JSON результата, включая `season_id`, `start_time`, `oldi_rating`, `newi_rating`, `car_name`, `track_name` и строки гонщиков.
3. `https://iracing6-backend.herokuapp.com/api/series-basic-info/all-seasons` — локально кэшируемое соответствие season/category.
4. `https://iracing6-backend.herokuapp.com/api/series-basic-info/series-basic-info/{season_name}` — расписание сезона с `race_week_num`, `start_date` и трассой.

Публичный iRacingData backend фактически является community cache: приложение читает его без авторизации, но не пытается обращаться к iRacing напрямую.

## Установка

Требуется Python 3.12+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## CLI

Первичный полный импорт:

```powershell
python main.py sync --cust-id 123456 --full-rescan
```

Короткая синхронизация после первичного импорта:

```powershell
python main.py sync --cust-id 123456
```

Совместимый shortcut:

```powershell
python sync.py --cust-id 123456
```

По умолчанию SQLite создаётся в `iracing.db`. Путь можно изменить:

```powershell
python main.py sync --cust-id 123456 --db-path data\iracing.db
```

Обычный `Sync` читает только первую страницу irstats (50 последних гонок). `Full rescan` читает историю строго последовательно, с задержкой 6 секунд между страницами. Задержку можно изменить через `IRSTATS_PAGE_DELAY`, но concurrency для страниц irstats не используется. Затем отсутствующие детали запрашиваются максимум двумя параллельными workers только с iRacingData backend. Каждый индекс и результат сохраняется сразу, поэтому остановка процесса не уничтожает уже импортированный прогресс.

Если обычный HTML-запрос irstats получает HTTP 403, приложение открывает только публичную страницу irstats в обычном видимом persistent Playwright browser context и ждёт, пока пользователь при необходимости завершит стандартную проверку Cloudflare. Никакие stealth/fingerprint/proxy/CAPTCHA bypass механизмы не используются. Профиль сохраняется локально в `%USERPROFILE%\.iracing-weekly-tracker\irstats-browser-profile`.

Fallback по умолчанию запускает установленный системный Chrome. Если Chrome отсутствует, можно установить bundled Chromium командой `playwright install chromium` и передать `browser_channel=None` при создании клиента.

После первого direct HTTP 403 или Cloudflare client переключается в browser-only mode до конца текущего процесса Sync; persistent Playwright context переиспользуется для всех следующих страниц. `--full-rescan` возобновляется со страницы после `last_successful_irstats_page`, если предыдущий запуск был прерван rate limit. После завершения обхода этот checkpoint помечается завершённым.

При HTTP 429 быстрые retry не выполняются. Успешные страницы и найденные `subsession_id` сохраняются в локальный `irstats_index`; UI показывает сохранённый прогресс и предлагает `Resume import` примерно через 60 секунд.

## Web UI

```powershell
python main.py serve
```

На Windows можно запустить приложение двойным кликом по `run_tracker.bat`.

Открыть: http://127.0.0.1:8000

Настройки Customer ID: http://127.0.0.1:8000/settings

UI включает:

- выбор категории, сезона, серии, машины и трассы;
- карточки current iRating, season change, races, wins, podiums и average SOF;
- weekly chart и переключатель Every race;
- раскрываемые race week с деталями каждой гонки;
- статус `Verified` или `Incomplete data`;
- `/debug` с временными метками sync, границами известной истории и последним subsession.

## Нормализация позиций

iRacingData возвращает `starting_position` и `finish_position` с нуля. В `RaceResult` и SQLite сохраняются оба варианта:

- `start_position_api` / `finish_position_api` — исходные значения API;
- `start_position` / `finish_position` — человекочитаемые значения, увеличенные на один.

Practice и Qualifying никогда не сохраняются: adapter выбирает только `simsession_name == "Race"` и нужный `cust_id`.

## Race week

Приложение не группирует гонки по ISO calendar week. Для каждой найденной `season_name` загружается season schedule. `race_week_num` берётся из schedule metadata; в UI он показывается как `race_week_num + 1`, поэтому API Week 0 становится iRacing Week 1.

В SQLite сохраняются `season_id`, `season_year`, `season_quarter`, `race_week_num` и `race_week_source`. Season/category и schedule metadata кэшируются локально, чтобы повторный анализ не зависел от повторного сетевого запроса.

Если schedule metadata недоступна или не даёт week/category, приложение оставляет поле неполным. Отдельные race-страницы irstats не запрашиваются: точные результат, iRating, трасса, машина и SOF по-прежнему берутся из iRacingData. Приложение не подменяет это ISO-неделей и не выдумывает значение.

## Continuity и incomplete history

Для одной license category соседние официальные гонки проверяются так:

```text
previous.new_irating == current.old_irating
```

Если значения расходятся, поле недели становится `Incomplete data`, а пропущенная гонка не восстанавливается искусственно. Дополнительно проверяется равенство:

```text
week_end - week_start == sum(race.new_irating - race.old_irating)
```

## Ограничения источников

- irstats может дозаполнять старые сезоны постепенно.
- irstats иногда отвечает HTTP 403 от Cloudflare. Клиент останавливается и сохраняет уже загруженные данные.
- CAPTCHA bypass, stealth plugins, proxy rotation и обход rate limits не используются.
- Публичного гарантированного SLA или rate limit для community sources нет; запросы идут с малой concurrency, retries и exponential backoff.
- iRating и season metadata считаются точными только настолько, насколько community cache доступен на момент синхронизации.

## Архитектура

```text
app/
  analytics/weeks.py       # race week grouping, deltas, continuity
  database/repository.py   # SQLite schema and UPSERT repository
  models/normalized.py     # RaceResult
  services/irstats.py      # sequential index fetch + BeautifulSoup parser
  services/iracingdata.py  # exact detail + schedule/category metadata
  services/http.py         # bounded retries and 403/429 handling
  services/sync.py         # resumable full/quick sync
  web/routes.py            # FastAPI routes
templates/                 # Jinja2 dark dashboard
static/                    # CSS and Chart.js integration
tests/                     # pytest unit and web tests
```

## Tests

```powershell
\.venv\Scripts\python.exe -m pytest -q
```

Unit tests используют mock payloads. Живой smoke-test источников можно выполнить отдельно, но полный импорт следует запускать только для нужного Customer ID и с уважением к ограничениям источников.
