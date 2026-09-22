#!/usr/bin/env bash
# Одна кнопка: окружение, зависимости, настройка и запуск.
#
# Первый запуск: создаст .venv, поставит зависимости, спросит токен бота.
# Все следующие: просто поднимет сервис. Останов — Ctrl+C.
#
#   ./start.sh              обычный запуск (перезапускается сам при падении)
#   ./start.sh --no-restart один прогон, без авто-перезапуска
#   ./start.sh --setup      перенастроить токен заново

set -u

cd "$(dirname "$0")" || exit 1

VENV="./.venv"
PROJECT="wb_monitor"
STAMP="$VENV/.requirements.sha"
RESTART=1
FORCE_SETUP=0

for arg in "$@"; do
  case "$arg" in
    --no-restart) RESTART=0 ;;
    --setup) FORCE_SETUP=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg"; exit 2 ;;
  esac
done

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
err() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

# ------------------------------------------------- 1. интерпретатор Python

find_python() {
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  err "Нужен Python 3.11 или новее, а его нет."
  case "$(uname -s)" in
    Darwin) err "Поставь: brew install python@3.12   (или с python.org)" ;;
    Linux)  err "Поставь: sudo apt install python3.12 python3.12-venv" ;;
  esac
  exit 1
}
say "Python: $("$PY" --version 2>&1)"

# --------------------------------------------- 2. виртуальное окружение

if [ ! -x "$VENV/bin/python" ]; then
  say "Создаю виртуальное окружение…"
  "$PY" -m venv "$VENV" || {
    err "Не удалось создать venv. На Debian/Ubuntu поставь пакет python3-venv."
    exit 1
  }
fi
VPY="$VENV/bin/python"

# ---------------------------------------------------- 3. зависимости

WANT="$("$VPY" - <<'PY'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())
PY
)"
HAVE="$(cat "$STAMP" 2>/dev/null || echo none)"

if [ "$WANT" != "$HAVE" ]; then
  say "Ставлю зависимости (первый раз это пара минут)…"
  "$VPY" -m pip install --quiet --upgrade pip || true
  if "$VPY" -m pip install --quiet -r requirements.txt; then
    echo "$WANT" > "$STAMP"
  else
    err "Зависимости не установились. Проверь интернет и запусти ещё раз."
    exit 1
  fi
fi

# ------------------------------------------------------- 4. настройка

if [ "$FORCE_SETUP" = "1" ]; then
  rm -f "$PROJECT/.env"
fi

if [ ! -f "$PROJECT/.env" ]; then
  "$VPY" "$PROJECT/setup_wizard.py" || exit 1
fi

# --------------------------------------------------------- 5. база

if ! db_log="$("$VPY" "$PROJECT/cli.py" init-db 2>&1)"; then
  err "Не удалось создать базу данных. Что сказал Python:"
  printf '%s\n' "$db_log" >&2
  case "$db_log" in
    *greenlet*)
      err ""
      err "Не хватает библиотеки greenlet. Лечится одной командой:"
      err "  $VPY -m pip install greenlet"
      ;;
  esac
  exit 1
fi

# --------------------------------------------------------- 6. запуск

say ""
say "Готово. Сервис работает — открой своего бота в Telegram и нажми /start."
say "Остановить: Ctrl+C"
say ""

trap 'echo; say "Остановлено."; exit 0' INT TERM

while :; do
  "$VPY" "$PROJECT/cli.py" run
  code=$?
  [ "$RESTART" = "1" ] || exit "$code"
  [ "$code" = "0" ] && exit 0
  if [ "$code" = "2" ]; then
    # ошибка настройки: перезапускать бессмысленно, текст уже показан выше
    exit 2
  fi
  err "Процесс упал (код $code). Перезапускаю через 10 секунд… (Ctrl+C — выйти)"
  sleep 10
done
