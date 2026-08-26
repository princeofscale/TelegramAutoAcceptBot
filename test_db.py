"""Самопроверка. Запуск: python test_db.py

Проверяем ровно то, что может сломаться молча: сопоставление вступлений
с отписками (люди уходят и возвращаются по нескольку раз) и загрузку локалей.
"""
import asyncio
import os
import tempfile
import time

os.environ.setdefault("BOT_TOKEN", "test")
os.environ.setdefault("HASH_SALT", "test-salt")

import config  # noqa: E402
import db  # noqa: E402


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        await db.connect(os.path.join(tmp, "t.db"))
        try:

            # хеш детерминирован и необратим на глаз
            assert db.user_hash(777) == db.user_hash(777)
            assert db.user_hash(777) != db.user_hash(778)
            assert "777" not in db.user_hash(777)

            # год аккаунта растёт вместе с id и не падает за границами таблицы
            assert db.account_year(50_000_000) == 2013
            assert db.account_year(1_600_000_000) == 2021
            assert db.account_year(9_999_999_999) == 2026
            years = [db.account_year(uid) for uid in (10**8, 10**9, 5 * 10**9, 8 * 10**9)]
            assert years == sorted(years), years

            CHAT = -100123
            await db.upsert_channel(CHAT, owner_id=1, title="Канал")
            assert (await db.get_channel(CHAT))["auto_approve"] == 0, "автоприём выключен по умолчанию"

            await db.set_auto_approve(CHAT, True)
            assert [c["chat_id"] for c in await db.channels_of(1)] == [CHAT]

            # Двое пришли по «закупу», один по «посеву».
            for uid in (101, 102):
                await db.record_join(CHAT, uid, "t.me/+aaa", "закуп", "ru", False, True)
            await db.record_join(CHAT, 201, "t.me/+bbb", "посев", "en", True, True)
            # Ещё один пришёл, но принять не удалось — в «принято» попасть не должен.
            await db.record_join(CHAT, 301, "t.me/+bbb", "посев", "ru", False, False)

            # 101 ушёл дважды: уход не должен удвоить его вступление.
            await db.record_leave(CHAT, 101)
            await db.record_leave(CHAT, 101)

            report = await db.stats(CHAT, days=7)
            totals = report["totals"]
            assert totals["requests"] == 4, totals
            assert totals["approved"] == 3, totals
            assert totals["gone"] == 1, f"повторный выход раздул счётчик: {totals}"
            assert totals["premium"] == 1, totals

            links = {row["label"]: row for row in report["links"]}
            assert links["закуп"]["joined"] == 2 and links["закуп"]["gone"] == 1, links
            # у «посева» в отчёт идёт только принятая заявка
            assert links["посев"]["joined"] == 1 and links["посев"]["gone"] == 0, links

            # Ссылки, созданные не ботом, Telegram отдаёт обрезанными: разные
            # связки различимы только по имени.
            await db.record_join(CHAT, 401, "https://t.me/+…", "тизер", "ru", False, True)
            await db.record_join(CHAT, 402, "https://t.me/+…", "баннер", "ru", False, True)
            labels = {row["label"] for row in (await db.stats(CHAT, days=7))["links"]}
            assert {"тизер", "баннер"} <= labels, f"обрезанные ссылки схлопнулись: {labels}"

            # Повторно присланный апдейт той же заявки не задваивает вступление.
            now = int(time.time())
            before = (await db.stats(CHAT, days=7))["totals"]["requests"]
            for _ in range(2):
                await db.record_join(CHAT, 501, "t.me/+ccc", "повтор", "ru", False, True, now)
            after = (await db.stats(CHAT, days=7))["totals"]["requests"]
            assert after == before + 1, f"повтор апдейта посчитан дважды: {before} → {after}"

            # Уход, записанный ДО вступления, не считается отпиской этого вступления.
            await db._db.execute(
                "INSERT INTO leave_events (chat_id, user_hash, ts) VALUES (?, ?, ?)",
                (CHAT, db.user_hash(202), int(time.time()) - 3600),
            )
            await db.record_join(CHAT, 202, "t.me/+bbb", "посев", "ru", False, True)
            assert (await db.stats(CHAT, days=7))["totals"]["gone"] == 1, "старый выход посчитан заново"

            # Удаление бота не стирает статистику
            assert await db.deactivate_channel(CHAT) == 1
            assert await db.channels_of(1) == []
            assert (await db.stats(CHAT, days=7))["totals"]["requests"] == 8

        finally:
            await db.close()

    # все локали на месте и переведены
    from aiogram.utils.i18n import I18n

    i18n = I18n(path=config.LOCALES_DIR, default_locale="ru", domain="messages")
    assert set(config.AVAILABLE_LOCALES) <= set(i18n.available_locales), i18n.available_locales
    with i18n.context(), i18n.use_locale("ru"):
        from aiogram.utils.i18n import gettext

        assert gettext("Requests") == "Заявок"
    with i18n.context(), i18n.use_locale("en"):
        assert gettext("Requests") == "Requests"

    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
