"""Единственный модуль, знающий про схему и SQL.

SQLite в WAL: писатель у нас один (бот — один процесс, polling), поэтому
конкурентной записи не бывает, а веб-часть Mini App сможет читать параллельно.
Весь SQL держим здесь — переезд на Postgres, если он когда-нибудь понадобится,
это переписать один файл.
"""
import hashlib
import hmac
import time
from datetime import datetime, timezone

import aiosqlite

import config

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    lang       TEXT    NOT NULL DEFAULT 'ru',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS channels (
    chat_id      INTEGER PRIMARY KEY,
    owner_id     INTEGER NOT NULL,
    title        TEXT,
    auto_approve INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1,
    connected_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_owner ON channels(owner_id);

-- О заявителях храним только то, что агрегируется в отчёт: необратимый хеш
-- вместо user_id, язык, признак Premium и год регистрации аккаунта.
-- Ни username, ни имени, ни bio — по ним человек находится за секунду,
-- а ни в одну строку статистики они не ложатся.
CREATE TABLE IF NOT EXISTS join_events (
    id         INTEGER PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    user_hash  TEXT    NOT NULL,
    link_url   TEXT,
    link_name  TEXT,
    lang       TEXT,
    is_premium INTEGER NOT NULL DEFAULT 0,
    acct_year  INTEGER,
    approved   INTEGER NOT NULL DEFAULT 0,
    ts         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_join_chat_ts ON join_events(chat_id, ts);

CREATE TABLE IF NOT EXISTS leave_events (
    id        INTEGER PRIMARY KEY,
    chat_id   INTEGER NOT NULL,
    user_hash TEXT    NOT NULL,
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leave_lookup ON leave_events(chat_id, user_hash, ts);
"""

_db: aiosqlite.Connection | None = None


async def connect(path: str | None = None) -> None:
    global _db
    _db = await aiosqlite.connect(path or config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(_SCHEMA)
    # ponytail: миграции через user_version. Пока версия одна, ветвление
    # добавляем в тот день, когда появится вторая.
    await _db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    await _db.commit()


async def close() -> None:
    if _db is not None:
        await _db.close()


# --- идентификаторы заявителей -------------------------------------------------

def user_hash(user_id: int) -> str:
    """Необратимый ключ для сопоставления «вступил» и «вышел».

    64 бита: на миллионах записей вероятность коллизии пренебрежима, а обратно
    из хеша user_id не достать даже зная соль — только перебором по всему
    пространству идентификаторов.
    """
    return hmac.new(config.HASH_SALT, str(user_id).encode(), hashlib.sha256).hexdigest()[:16]


# ponytail: user_id в Telegram растёт монотонно, поэтому по нему выводится
# приблизительный год регистрации. Пороги прикинуты по эмпирике и со временем
# уезжают — это калибровочная ручка, правится добавлением строки в конец.
_ID_EPOCHS = (
    (0, 2013),
    (100_000_000, 2016),
    (300_000_000, 2017),
    (500_000_000, 2018),
    (800_000_000, 2019),
    (1_100_000_000, 2020),
    (1_500_000_000, 2021),
    (2_000_000_000, 2022),
    (5_000_000_000, 2023),
    (6_000_000_000, 2024),
    (7_300_000_000, 2025),
    (8_200_000_000, 2026),
)


def account_year(user_id: int) -> int:
    year = _ID_EPOCHS[0][1]
    for threshold, epoch_year in _ID_EPOCHS:
        if user_id < threshold:
            break
        year = epoch_year
    return year


# --- пользователи --------------------------------------------------------------

async def ensure_user(user_id: int, language_code: str | None) -> None:
    lang = language_code if language_code in config.AVAILABLE_LOCALES else config.DEFAULT_LOCALE
    await _db.execute(
        "INSERT OR IGNORE INTO users (user_id, lang, created_at) VALUES (?, ?, ?)",
        (user_id, lang, int(time.time())),
    )
    await _db.commit()


async def get_lang(user_id: int) -> str | None:
    async with _db.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return row["lang"] if row else None


async def set_lang(user_id: int, lang: str) -> None:
    await _db.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
    await _db.commit()


# --- каналы --------------------------------------------------------------------

async def upsert_channel(chat_id: int, owner_id: int, title: str | None) -> None:
    """Повторное подключение возвращает канал в строй, не трогая настройку тумблера."""
    await _db.execute(
        """INSERT INTO channels (chat_id, owner_id, title, connected_at)
                VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                title    = excluded.title,
                active   = 1""",
        (chat_id, owner_id, title, int(time.time())),
    )
    await _db.commit()


async def get_channel(chat_id: int) -> aiosqlite.Row | None:
    async with _db.execute("SELECT * FROM channels WHERE chat_id = ?", (chat_id,)) as cur:
        return await cur.fetchone()


async def channels_of(owner_id: int) -> list[aiosqlite.Row]:
    async with _db.execute(
        "SELECT * FROM channels WHERE owner_id = ? AND active = 1 ORDER BY connected_at",
        (owner_id,),
    ) as cur:
        return list(await cur.fetchall())


async def set_auto_approve(chat_id: int, enabled: bool) -> None:
    await _db.execute(
        "UPDATE channels SET auto_approve = ? WHERE chat_id = ?", (int(enabled), chat_id)
    )
    await _db.commit()


async def deactivate_channel(chat_id: int) -> int | None:
    """Помечаем неактивным, но не чистим: вернут бота — статистика на месте."""
    channel = await get_channel(chat_id)
    if channel is None:
        return None
    await _db.execute("UPDATE channels SET active = 0 WHERE chat_id = ?", (chat_id,))
    await _db.commit()
    return channel["owner_id"]


# --- события -------------------------------------------------------------------

async def record_join(
    chat_id: int,
    user_id: int,
    link_url: str | None,
    link_name: str | None,
    lang: str | None,
    is_premium: bool,
    approved: bool,
) -> None:
    await _db.execute(
        """INSERT INTO join_events
               (chat_id, user_hash, link_url, link_name, lang, is_premium, acct_year, approved, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            chat_id,
            user_hash(user_id),
            link_url,
            link_name,
            lang,
            int(is_premium),
            account_year(user_id),
            int(approved),
            int(time.time()),
        ),
    )
    await _db.commit()


async def record_leave(chat_id: int, user_id: int) -> None:
    await _db.execute(
        "INSERT INTO leave_events (chat_id, user_hash, ts) VALUES (?, ?, ?)",
        (chat_id, user_hash(user_id), int(time.time())),
    )
    await _db.commit()


# --- отчёт ---------------------------------------------------------------------

async def stats(chat_id: int, days: int) -> dict:
    """Сводка за период: объёмы, разбивка по инвайт-ссылкам, портрет аудитории.

    «Ушёл» считается через EXISTS, а не JOIN: человек мог выходить и возвращаться
    несколько раз, и JOIN размножил бы его вступление на каждый выход.

    Сравнение времени нестрогое: вступление и выход укладываются в одну секунду
    чаще, чем кажется, и при `>` такие отписки терялись бы целиком.
    """
    since = int(time.time()) - days * 86400
    fresh_from = datetime.now(timezone.utc).year - 1

    async with _db.execute(
        """SELECT COUNT(*) AS requests,
                  COALESCE(SUM(approved), 0) AS approved,
                  COALESCE(SUM(is_premium), 0) AS premium,
                  COALESCE(SUM(CASE WHEN acct_year >= ? THEN 1 ELSE 0 END), 0) AS fresh
             FROM join_events
            WHERE chat_id = ? AND ts >= ?""",
        (fresh_from, chat_id, since),
    ) as cur:
        totals = dict(await cur.fetchone())

    async with _db.execute(
        """SELECT COALESCE(j.link_name, j.link_url, '?') AS label,
                  COUNT(*) AS joined,
                  SUM(EXISTS(SELECT 1 FROM leave_events l
                              WHERE l.chat_id = j.chat_id
                                AND l.user_hash = j.user_hash
                                AND l.ts >= j.ts)) AS gone
             FROM join_events j
            WHERE j.chat_id = ? AND j.ts >= ? AND j.approved = 1
         GROUP BY COALESCE(j.link_url, '?')
         ORDER BY joined DESC""",
        (chat_id, since),
    ) as cur:
        links = [dict(row) for row in await cur.fetchall()]

    totals["gone"] = sum(link["gone"] for link in links)
    return {"totals": totals, "links": links, "days": days}
