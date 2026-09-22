# WB Monitor — мониторинг конкурентов на Wildberries

Следит за ценами и остатками конкурентов на WB: пишет в Telegram, когда
конкурент уронил цену или обнулил остаток, и раз в неделю присылает
Excel-отчёт. Работает сам, вмешательства не требует.

`Python 3.11+` · `aiogram 3` · `SQLAlchemy 2 (async)` · `APScheduler` ·
`httpx` · `openpyxl` · `SQLite` · `pytest` — 93 теста

---

## Запуск: одна кнопка

**macOS** — два клика по `start.command`
**Windows** — два клика по `start.bat`
**Linux** — `./start.sh` в терминале

Скрипт сам создаст окружение, поставит зависимости, спросит токен бота и
запустит сервис. Всё, что нужно от тебя — **токен**:

1. Открой в Telegram [@BotFather](https://t.me/BotFather)
2. Отправь `/newbot`, придумай имя и username (должен кончаться на `bot`)
3. Скопируй строку вида `8123456789:AAHdqTcvCH1vGW…` и вставь в окно запуска

Дальше найди своего бота в Telegram и нажми **/start** — этот чат
автоматически станет админским (искать chat_id через @userinfobot не надо).

> Первый запуск занимает пару минут: ставятся библиотеки. Следующие —
> пара секунд. Остановить сервис — `Ctrl+C` в окне.

### Дальше всё кнопками

| Кнопка | Что делает |
|---|---|
| ➕ Добавить товар | присылаешь ссылку на карточку WB или артикул — бот находит товар и спрашивает, свой он или конкурента |
| 📋 Мои товары | что отслеживается, с текущей ценой и остатком; там же пауза и удаление |
| 🔄 Проверить сейчас | снять цены немедленно, не дожидаясь расписания |
| 📊 Отчёт | Excel за последние 7 дней прямо в чат |
| ⚙️ Статус | когда был последний проход, сколько замеров, жив ли парсер |

Ссылку можно прислать и без кнопки — бот поймёт.

**Важно:** алерты начинаются со **второго** замера по товару — первому
не с чем сравнивать. Добавил товары → нажми «🔄 Проверить сейчас» → дальше
сервис работает по расписанию сам.

---

## Что приходит в Telegram

```
📉 «Носки х/б — БрендА»: цена упала на 18% (450 → 369 руб.)
⛔ «Термобельё — БрендБ»: остаток обнулился (было 23 шт.)
✅ «Шапка — БрендВ»: товар снова в наличии (40 шт.)
```

Раз в неделю (понедельник, 08:00 UTC) — файл `.xlsx` с двумя листами:
«Сводка» (главное за неделю) и «Цены и остатки» (таблица по всем SKU с
изменением цены и статусом).

---

## Если что-то идёт не так

```bash
.venv/bin/python wb_monitor/cli.py doctor
```

Проверит разом токен, связь с Wildberries, базу, админский чат и наличие
замеров — и скажет, что именно чинить.

Частое:

| Симптом | Причина и что делать |
|---|---|
| «Telegram отклонил BOT_TOKEN» | токен скопирован с обрезкой → `./start.sh --setup` |
| Бот молчит на /start | сервис не запущен — проверь окно, где нажимал кнопку |
| «WB не отвечает» в боте | WB режет твой IP: пропиши `PROXY_URL` в `.env` |
| Пришёл алерт «похоже, сломался парсер» | WB сменил разметку ответа → правится в `parser/wb_api.py` |

Алерты клиентам при сломанном парсере подавляются автоматически: массовый
«остаток 0» — это почти всегда не рынок, а изменившаяся вёрстка WB.

---

## Настройки

Всё лежит в `.env` (создаётся при первом запуске):

```bash
PARSE_INTERVAL_HOURS=4   # как часто снимать цены
REPORT_WEEKDAY=mon       # день недельного отчёта
REPORT_HOUR=8            # час отчёта, UTC
PROXY_URL=               # прокси для парсера, когда WB начнёт банить IP
```

Пороги срабатывания алертов (по умолчанию ±10% по цене) — в
`scheduler/thresholds.py`, там же порог «мало остатка» для отчёта.

После правок перезапусти сервис.

---

## Как это устроено

Один процесс (`cli.py run`): Telegram-бот на aiogram + планировщик
APScheduler. База — один файл SQLite.

| Job | Когда | Что делает |
|---|---|---|
| `run_parse_cycle` | каждые 4 ч | parse → health-check → алерты |
| `parse_job` | внутри цикла | обходит активные SKU, пишет `PriceSnapshot`; паузы 0.5–1.5 с между запросами |
| `alert_check_job` | внутри цикла | сравнивает два последних снепшота: обвал/рост цены >10%, обнуление остатка, возврат в наличие |
| `health_check_job` | после parse + ежедневно | >30% ошибок или массовые нули за проход → алерт **админу**, клиентские алерты подавляются |
| `weekly_report_job` | понедельник 08:00 UTC | Excel-отчёт за неделю каждому активному клиенту |

Антидубль алертов: каждый `Alert` хранит `snapshot_id`, на котором сработал
триггер. Повторный прогон по тем же данным молчит; повторный алерт того же
типа возможен только после обратного перехода состояния (тесты в
`tests/test_alerts.py`).

```
start.sh / start.command / start.bat     # одна кнопка
requirements.txt
wb_monitor/
  setup_wizard.py      # спрашивает токен, проверяет его у Telegram
  models.py            # Client, TrackedSKU, PriceSnapshot, Alert, ParseRun, AppSetting
  db.py                # движок/сессии
  config.py            # .env-конфиг
  state.py             # admin_chat_id, который бот запоминает сам
  app.py               # процесс: бот + APScheduler
  services.py          # общие зависимости для бота и job'ов
  parser/
    client.py          # httpx с delay/retry, прокси-ready
    wb_api.py          # цена/остаток по nmId, с запасными эндпоинтами
    wb_link.py         # артикул из ссылки/числа/текста
  scheduler/
    jobs.py            # parse / alerts / health / weekly report
    thresholds.py      # все пороги константами
  reports/excel_report.py
  bot/
    menu.py            # кнопочный интерфейс (основной путь)
    keyboards.py
    handlers.py        # текстовые команды для работы с клиентами
    notify.py
  cli.py               # ручной запуск job'ов, doctor, администрирование
  deploy/wb-monitor.service
  tests/               # 93 теста
```

---

## Обслуживание нескольких клиентов

Сервис изначально подписочный: можно вести не только свои товары, но и
чужие. Из админского чата (`/admin` — список команд):

```
/add_client Имя; chat_id; basic|standard|pro; цена_руб
/add_sku <client_id> <nmId> <название> [own]
/clients
/skus <client_id>
/report <client_id>
```

Клиент узнаёт свой `chat_id`, написав боту `/start`. Тарифы: `basic` —
только недельный отчёт, `standard`/`pro` — плюс Telegram-алерты.

То же самое из командной строки:

```bash
.venv/bin/python wb_monitor/cli.py add-client --name "ИП Иванов" --chat-id 123456789 --tier standard --price 1500
.venv/bin/python wb_monitor/cli.py add-sku --client-id 1 --nm-id 18234561 --name "Носки" --own
.venv/bin/python wb_monitor/cli.py run-parse
.venv/bin/python wb_monitor/cli.py run-report --client-id 1 --dry-run
```

---

## Тесты

```bash
.venv/bin/python -m pytest
```

Покрыто: логика алертов и антидубль, health-check, структура отчёта,
разбор ответов WB, кнопочные сценарии бота целиком (через реальный
Dispatcher с подменённым Telegram) и старт процесса.

---

## Если хочется на сервер

Чтобы мониторинг работал, когда компьютер выключен — любой VPS,
Ubuntu/Debian:

```bash
# 1. Код
sudo useradd -r -m -d /opt/wb-monitor wbmon
sudo -u wbmon git clone https://github.com/privetante-ctrl/wb-monitor.git /opt/wb-monitor

# 2. Первый запуск создаст окружение и спросит токен
sudo -u wbmon /opt/wb-monitor/start.sh --no-restart
# ...нажми Ctrl+C после «Готово»

# 3. systemd — чтобы поднимался сам
sudo cp /opt/wb-monitor/wb_monitor/deploy/wb-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wb-monitor
journalctl -u wb-monitor -f     # логи
```

Бэкап — это копия одного файла:

```
0 3 * * * cp /opt/wb-monitor/wb_monitor/wb_monitor.db /opt/wb-monitor/backups/wb_$(date +\%F).db
```

Резервный план: если APScheduler окажется нестабильным, те же job'ы
дёргаются системным cron'ом через CLI (`cli.py run-parse && cli.py
run-alerts`) — код job'ов от планировщика не зависит.
