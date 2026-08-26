"""Конфигурация из окружения. Падаем на старте, а не в первом хендлере."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}. См. .env.example")
    return value


BOT_TOKEN = _required("BOT_TOKEN")

# Соль HMAC. Меняется -> вся накопленная статистика отписок рассыпается,
# потому что старые хеши перестают совпадать с новыми.
HASH_SALT = _required("HASH_SALT").encode()

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "bot.db"))

LOCALES_DIR = BASE_DIR / "locales"
DEFAULT_LOCALE = "ru"
AVAILABLE_LOCALES = ("ru", "en")
