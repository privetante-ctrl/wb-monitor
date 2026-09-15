# WB Monitor — мониторинг конкурентов на Wildberries

Подписочный сервис для мелких продавцов WB (1-20 SKU): еженедельный
Excel-отчёт по ценам/остаткам конкурентов + Telegram-алерты при резких
изменениях. Без веб-кабинета: доставка через Excel-файл и Telegram-бота.

## Как устроено

Один процесс (`python cli.py run`): aiogram-бот (polling) + APScheduler.

| Job | Расписание | Что делает |
|---|---|---|
| `run_parse_cycle` | каждые 4 ч (настраивается) | parse → health-check → алерты |
| `parse_job` | внутри цикла | обходит активные SKU, пишет `PriceSnapshot`; паузы 0.5-1.5 с между запросами |
| `alert_check_job` | внутри цикла | сравнивает 2 последних снепшота: обвал цены >10%, рост >10%, обнуление остатка, возврат в наличие. Только тарифы standard/pro |
| `health_check_job` | после parse + ежедневно | >30% ошибок или массовые нули за проход → алерт **админу**, клиентские алерты подавляются; следит, что parse вообще запускался |
| `weekly_report_job` | понедельник 08:00 UTC | Excel-отчёт за прошлую неделю каждому активному клиенту, файлом в чат |

Антидубль алертов: каждый `Alert` хранит `snapshot_id`, на котором сработал
триггер. Повторный прогон по тем же данным молчит; повторный алерт того же
типа возможен только после обратного перехода состояния (тесты в
`tests/test_alerts.py`).

Пороги — в `scheduler/thresholds.py`, расписание и токены — в `.env`.

## Быстрый старт локально

```bash
cd wb_monitor
python3.11 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заполнить BOT_TOKEN и ADMIN_CHAT_ID

python cli.py init-db
python cli.py add-client --name "ИП Иванов" --chat-id 123456789 --tier standard --price 1500
python cli.py add-sku --client-id 1 --nm-id 18234561 --name "Носки шерстяные" --own --category Носки
python cli.py add-sku --client-id 1 --nm-id 22190345 --name "Носки х/б - БрендА" --category Носки

python cli.py run-parse                  # один проход парсера
python cli.py run-alerts --dry-run       # проверить триггеры без отправки
python cli.py run-report --client-id 1 --dry-run   # файл в generated_reports/
python cli.py run                        # бот + планировщик (боевой режим)
```

`BOT_TOKEN` выдаёт [@BotFather](https://t.me/BotFather) (`/newbot`).
`ADMIN_CHAT_ID` — твой личный chat_id, проще всего узнать у
[@userinfobot](https://t.me/userinfobot); сюда идут технические алерты
(«похоже, сломался парсер»), клиенты их не видят.

Команды бота (только из админского чата): `/add_client`, `/add_sku`,
`/clients`, `/skus`, `/report` — см. `/help` в боте. Клиент, написавший
боту `/start`, получает свой chat_id — этим id его и регистрируешь.

## Тесты

```bash
cd wb_monitor
pytest            # логика алертов, антидубль, health-check, структура отчёта
```

## Деплой на VPS (Ubuntu/Debian, systemd)

```bash
# 1. Пользователь и код
sudo useradd -r -m -d /opt/wb_monitor wbmon
sudo -u wbmon git clone <репозиторий> /opt/wb_monitor/src   # или scp папки wb_monitor
# далее считаем, что код лежит в /opt/wb_monitor/wb_monitor

# 2. Окружение
sudo -u wbmon python3.11 -m venv /opt/wb_monitor/venv
sudo -u wbmon /opt/wb_monitor/venv/bin/pip install -r /opt/wb_monitor/wb_monitor/requirements.txt

# 3. Конфиг
sudo -u wbmon cp /opt/wb_monitor/wb_monitor/.env.example /opt/wb_monitor/wb_monitor/.env
sudo -u wbmon nano /opt/wb_monitor/wb_monitor/.env   # BOT_TOKEN, ADMIN_CHAT_ID

# 4. БД
sudo -u wbmon /opt/wb_monitor/venv/bin/python /opt/wb_monitor/wb_monitor/cli.py init-db

# 5. systemd
sudo cp /opt/wb_monitor/wb_monitor/deploy/wb-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wb-monitor

# Логи
journalctl -u wb-monitor -f
```

Если пути отличаются — поправь `WorkingDirectory`/`ExecStart` в юните.

Резервный план из architecture.md: если процесс с APScheduler окажется
нестабильным, те же job'ы дёргаются системным cron'ом через CLI
(`cli.py run-parse && cli.py run-alerts`, `cli.py run-report`) — код
job'ов от планировщика не зависит.

## Бэкап

Вся база — один файл SQLite (`wb_monitor.db`). Достаточно cron-строчки:

```
0 3 * * * cp /opt/wb_monitor/wb_monitor/wb_monitor.db /opt/wb_monitor/backups/wb_monitor_$(date +\%F).db
```

## Прокси (когда WB начнёт банить IP)

Задай `PROXY_URL` в `.env` — весь трафик парсера пойдёт через прокси без
правок кода (`parser/client.py`, конструктор `HttpClient`). Пул прокси с
ротацией — тоже правка только этого класса.

## Структура

```
wb_monitor/
  models.py            # SQLAlchemy async: Client, TrackedSKU, PriceSnapshot, Alert, ParseRun
  db.py                # движок/сессии
  config.py            # .env-конфиг
  app.py               # процесс: бот + APScheduler
  parser/
    client.py          # httpx с delay/retry, прокси-ready
    wb_api.py          # цена/остаток по nmId (card.wb.ru)
  scheduler/
    jobs.py            # parse / alerts / health / weekly report
    thresholds.py      # все пороги констанами
  reports/
    excel_report.py    # генерация отчёта (структура = sample_report.xlsx)
  bot/
    handlers.py        # админ-команды
    notify.py          # отправка алертов/файлов
  cli.py               # ручной запуск job'ов, добавление клиентов/SKU
  deploy/wb-monitor.service
  tests/
```
