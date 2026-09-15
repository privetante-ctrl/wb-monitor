"""Конфигурация через .env (python-dotenv).

Ищем .env сначала рядом с этим файлом (wb_monitor/.env), затем в cwd —
чтобы работали и `python cli.py ...` из папки проекта, и systemd с
WorkingDirectory на корень репозитория.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).resolve().parent

load_dotenv(_PROJECT_DIR / ".env")
load_dotenv()  # .env в cwd, если есть — не перекрывает уже загруженные значения


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


@dataclass
class Settings:
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    # твой личный chat_id: сюда идут технические алерты (scraper_broken и т.п.)
    admin_chat_id: int = field(default_factory=lambda: _env_int("ADMIN_CHAT_ID", 0))

    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", str(_PROJECT_DIR / "wb_monitor.db"))
    )

    # интервалы job'ов (см. architecture.md)
    parse_interval_hours: int = field(default_factory=lambda: _env_int("PARSE_INTERVAL_HOURS", 4))
    report_weekday: str = field(default_factory=lambda: os.getenv("REPORT_WEEKDAY", "mon"))
    report_hour: int = field(default_factory=lambda: _env_int("REPORT_HOUR", 8))
    health_check_hour: int = field(default_factory=lambda: _env_int("HEALTH_CHECK_HOUR", 9))

    # парсер
    request_delay_min: float = field(default_factory=lambda: _env_float("REQUEST_DELAY_MIN", 0.5))
    request_delay_max: float = field(default_factory=lambda: _env_float("REQUEST_DELAY_MAX", 1.5))
    request_timeout: float = field(default_factory=lambda: _env_float("REQUEST_TIMEOUT", 10.0))
    request_retries: int = field(default_factory=lambda: _env_int("REQUEST_RETRIES", 3))
    # точка расширения: прокси не подключаем сейчас, но URL можно задать через .env
    proxy_url: str | None = field(default_factory=lambda: os.getenv("PROXY_URL") or None)

    # куда складывать сгенерированные отчёты (кроме отправки в Telegram)
    reports_dir: str = field(
        default_factory=lambda: os.getenv("REPORTS_DIR", str(_PROJECT_DIR / "generated_reports"))
    )

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"


settings = Settings()
