#!/usr/bin/env bash
# macOS: двойной клик по этому файлу запускает сервис.
# (Если Finder ругается «не удалось открыть» — один раз выполни в Терминале:
#  chmod +x start.command)
cd "$(dirname "$0")" || exit 1
./start.sh
echo
echo "Окно можно закрыть."
read -r -p "Нажми Enter…" _
