"""Интерактивная настройка: единственное, что нужно спросить у человека.

Вызывается из start.sh, когда .env ещё нет. Спрашивает ровно одно —
токен бота — и сразу проверяет его у Telegram, чтобы опечатка вылезла
здесь, а не через час молчания. ADMIN_CHAT_ID не спрашиваем: бот
запомнит первого, кто нажмёт /start (см. state.AdminRef).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")

ENV_TEMPLATE = """\
# Токен бота от @BotFather
BOT_TOKEN={token}

# Chat_id для технических алертов. 0 = бот запомнит первого, кто нажмёт /start.
ADMIN_CHAT_ID={admin_chat_id}

# Расписание
PARSE_INTERVAL_HOURS=4
REPORT_WEEKDAY=mon
REPORT_HOUR=8
HEALTH_CHECK_HOUR=9

# Парсер
REQUEST_DELAY_MIN=0.5
REQUEST_DELAY_MAX=1.5
REQUEST_TIMEOUT=10
REQUEST_RETRIES=3

# Прокси не нужен на старте; когда WB начнёт банить IP — раскомментируй:
#PROXY_URL=http://user:pass@host:port
"""

INTRO = """
╔══════════════════════════════════════════════════════════════╗
║   WB Monitor — настройка (это нужно сделать один раз)        ║
╚══════════════════════════════════════════════════════════════╝

Нужен токен Telegram-бота. Если его ещё нет:

  1. Открой в Telegram @BotFather
  2. Отправь ему:  /newbot
  3. Придумай имя (любое) и username (должен заканчиваться на bot)
  4. BotFather пришлёт строку вида
     8123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw

Скопируй её целиком и вставь сюда.
"""


def check_token(token: str) -> tuple[bool, str]:
    """Спросить у Telegram, живой ли токен. Возвращает (ок, описание)."""
    try:
        response = httpx.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=15.0
        )
    except httpx.HTTPError as exc:
        return False, f"нет связи с Telegram ({exc}). Проверь интернет."
    if response.status_code == 401:
        return False, "Telegram не признал этот токен. Скопируй его из @BotFather ещё раз."
    if response.status_code != 200:
        return False, f"Telegram ответил {response.status_code}. Попробуй ещё раз."
    username = (response.json().get("result") or {}).get("username", "?")
    return True, username


def ask_token() -> str:
    print(INTRO)
    while True:
        token = input("Токен бота: ").strip()
        if not token:
            print("  Пусто. Вставь строку от @BotFather.\n")
            continue
        if not TOKEN_RE.match(token):
            print("  Это не похоже на токен (формат «цифры:буквы»). Попробуй ещё раз.\n")
            continue
        print("  Проверяю у Telegram…")
        ok, info = check_token(token)
        if ok:
            print(f"  ✅ Бот @{info} на связи.\n")
            return token
        print(f"  ❌ {info}\n")


def write_env(token: str, admin_chat_id: int = 0) -> Path:
    ENV_PATH.write_text(
        ENV_TEMPLATE.format(token=token, admin_chat_id=admin_chat_id), encoding="utf-8"
    )
    ENV_PATH.chmod(0o600)  # в файле лежит токен — чужим читать незачем
    return ENV_PATH


def main() -> int:
    if ENV_PATH.exists():
        print(f"Настройка уже есть: {ENV_PATH}")
        print("Чтобы настроить заново — удали этот файл и запусти start.sh снова.")
        return 0
    try:
        token = ask_token()
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.")
        return 1
    write_env(token)
    print(f"Настройки сохранены: {ENV_PATH}\n")
    print("Дальше: найди своего бота в Telegram и нажми /start —")
    print("этот чат станет админским, туда пойдут алерты.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
