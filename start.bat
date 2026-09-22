@echo off
rem Windows: двойной клик по этому файлу запускает сервис.
setlocal
cd /d "%~dp0"

set "PY="
for %%V in (python3.14 python3.13 python3.12 python3.11 python) do (
  if not defined PY (
    %%V -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1 && set "PY=%%V"
  )
)
if not defined PY (
  echo Нужен Python 3.11 или новее. Поставь его с python.org
  echo и при установке отметь галочку "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Создаю виртуальное окружение...
  %PY% -m venv .venv || (echo Не удалось создать venv & pause & exit /b 1)
)
set "VPY=.venv\Scripts\python.exe"

echo Проверяю зависимости...
"%VPY%" -m pip install --quiet --upgrade pip
"%VPY%" -m pip install --quiet -r requirements.txt || (echo Зависимости не установились & pause & exit /b 1)

if not exist "wb_monitor\.env" (
  "%VPY%" wb_monitor\setup_wizard.py || (pause & exit /b 1)
)

"%VPY%" wb_monitor\cli.py init-db >nul || (echo Не удалось создать базу & pause & exit /b 1)

echo.
echo Готово. Открой своего бота в Telegram и нажми /start.
echo Остановить: Ctrl+C
echo.

:run
"%VPY%" wb_monitor\cli.py run
if errorlevel 1 (
  echo Процесс упал. Перезапуск через 10 секунд... Ctrl+C - выйти.
  timeout /t 10 >nul
  goto run
)
pause
