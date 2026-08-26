"""Конфигурация из окружения. Падаем на старте, а не в первом хендлере."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# .env читаем сами: ради трёх строк тащить python-dotenv незачем. Значения из
# окружения (systemd EnvironmentFile) имеют приоритет над файлом.
_ENV_FILE = BASE_DIR / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip().strip("\'\""))


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
