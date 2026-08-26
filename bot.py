"""Автоприём заявок в приватные Telegram-каналы.

Один общий бот на всех: человек добавляет его админом в свой канал и включает
тумблер. Личку заявителям бот не трогает — рассылки от общего аккаунта это то,
за что Telegram банит бота вместе со всеми подключёнными каналами.
"""
import asyncio
import html
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.i18n import I18n
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n.middleware import I18nMiddleware

import config
import db

router = Router()

# 1 — скользящие сутки, 0 — всё время.
PERIODS = (1, 7, 30, 0)
CARD_PERIOD = 7

# Сколько строк отчёта влезает, не упираясь в лимит сообщения Telegram.
MAX_LINK_ROWS = 10

# Пороги качества связки по доле оставшихся.
GOOD, FAIR = 80, 50


# --- i18n ----------------------------------------------------------------------

class DBI18nMiddleware(I18nMiddleware):
    """Язык из настроек пользователя; для незнакомых — из клиента Telegram."""

    async def get_locale(self, event: TelegramObject, data: dict) -> str:
        user = data.get("event_from_user")
        if user is None:
            return self.i18n.default_locale
        lang = await db.get_lang(user.id)
        if lang in config.AVAILABLE_LOCALES:
            return lang
        if user.language_code in config.AVAILABLE_LOCALES:
            return user.language_code
        return self.i18n.default_locale


# --- клавиатуры ----------------------------------------------------------------

def period_name(days: int) -> str:
    return {
        1: _("24 hours"),
        7: _("7 days"),
        30: _("30 days"),
        0: _("all time"),
    }[days]


def period_button(days: int) -> str:
    return {1: _("24h"), 7: _("7d"), 30: _("30d"), 0: _("all")}[days]


async def main_menu(owner_id: int) -> InlineKeyboardMarkup:
    rows = []
    for channel in await db.channels_of(owner_id):
        # ⛔ — бота выгнали. Канал остаётся в списке: отчёт за прошлое живой.
        mark = "✅" if channel["auto_approve"] else "⬜️"
        if not channel["active"]:
            mark = "⛔"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {channel['title'] or '?'}",
                    callback_data=f"ch:{channel['chat_id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=_("➕ How to connect"), callback_data="how")])
    # Подпись кнопки — сам язык, на который переключаемся: «Русский / English»
    # не говорит, какой сейчас включён.
    other = "EN" if await db.get_lang(owner_id) != "en" else "RU"
    rows.append([InlineKeyboardButton(text=other, callback_data="lang")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_menu(channel) -> InlineKeyboardMarkup:
    chat_id = channel["chat_id"]
    rows = []
    if channel["active"]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("🔴 Turn auto-approve off")
                    if channel["auto_approve"]
                    else _("🟢 Turn auto-approve on"),
                    callback_data=f"toggle:{chat_id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text=_("📊 Stats"), callback_data=f"stats:{chat_id}:7:r")]
    )
    rows.append([InlineKeyboardButton(text=_("⬅️ Back"), callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def report_menu(chat_id: int, days: int, sort: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    # Точкой помечен открытый период: отдельной строки состояния нет.
                    text=("· " if period == days else "") + period_button(period),
                    callback_data=f"stats:{chat_id}:{period}:{sort}",
                )
                for period in PERIODS
            ],
            [
                InlineKeyboardButton(
                    text=_("↕ by volume") if sort == "r" else _("↕ by retention"),
                    callback_data=f"stats:{chat_id}:{days}:{'v' if sort == 'r' else 'r'}",
                )
            ],
            [InlineKeyboardButton(text=_("⬅️ Back"), callback_data=f"ch:{chat_id}")],
        ]
    )


# --- вспомогательное -----------------------------------------------------------

async def notify(bot: Bot, user_id: int, text: str, reply_markup=None) -> None:
    """Написать владельцу. Молча пропускаем, если он не запускал бота."""
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except TelegramAPIError as exc:
        logging.info("не доставлено %s: %s", user_id, exc)


async def in_lang(user_id: int):
    """Контекст локали получателя.

    Локаль в мидлвари берётся от того, кто вызвал апдейт. Бота из канала может
    убрать другой админ — алерт владельцу не должен приходить на языке этого админа.
    """
    lang = await db.get_lang(user_id) or config.DEFAULT_LOCALE
    return I18n.get_current().use_locale(lang)


async def show(call: CallbackQuery, text: str, reply_markup=None) -> None:
    """edit_text, который не оставляет часики крутиться на кнопке."""
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        # Повторное нажатие той же кнопки — не ошибка.
        if "message is not modified" not in exc.message:
            raise
    finally:
        await call.answer()


async def owned_channel(call: CallbackQuery, chat_id: int):
    channel = await db.get_channel(chat_id)
    if channel is None or channel["owner_id"] != call.from_user.id:
        await call.answer(_("Channel not found."), show_alert=True)
        return None
    return channel


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


def link_label(raw: str) -> str:
    """В отчёт идёт имя связки. Сам инвайт — секрет, ему в переписке не место."""
    if raw == "?":
        return _("no name")
    label = raw.split("t.me/")[-1].lstrip("+")
    return label if len(label) <= 9 else label[:8] + "…"


async def channel_screen(channel) -> str:
    title = f"<b>{html.escape(channel['title'] or '?')}</b>"
    if not channel["active"]:
        return title + "\n\n" + _(
            "⛔ I am not in this channel any more, so new requests are not approved. "
            "The stats below are still yours."
        )

    report = await db.stats(channel["chat_id"], CARD_PERIOD)
    totals = report["totals"]
    if not totals["requests"]:
        return title + "\n\n" + _("No requests yet.")
    return title + "\n\n" + _("{days} days · requests {n} · stayed {kept}").format(
        days=CARD_PERIOD,
        n=totals["requests"],
        kept=pct(totals["approved"] - totals["gone"], totals["approved"]),
    )


def format_report(title: str, report: dict, auto_approve: bool) -> str:
    totals, links = report["totals"], report["links"]
    header = _("<b>{title} · {period}</b>").format(
        title=html.escape(title), period=period_name(report["days"])
    )

    if not totals["requests"]:
        return (
            header
            + "\n\n"
            + _("No requests in this period.")
            + "\n"
            + _(
                "If you have just switched auto-approve on, that is expected: "
                "requests that were already pending stay invisible to me."
            )
        )

    lines = [
        _("Requests {n} · stayed {kept}").format(
            n=totals["requests"],
            kept=pct(totals["approved"] - totals["gone"], totals["approved"]),
        ),
        _("Premium {premium} · new accounts {fresh}").format(
            premium=pct(totals["premium"], totals["requests"]),
            fresh=pct(totals["fresh"], totals["requests"]),
        ),
    ]
    if not auto_approve:
        lines.append(_("Auto-approve is off right now."))

    table = [_("            came  kept  new")]
    for link in links[:MAX_LINK_ROWS]:
        stayed = link["joined"] - link["gone"]
        kept = round(100 * stayed / link["joined"]) if link["joined"] else 0
        mark = "🟢" if kept >= GOOD else "🟡" if kept >= FAIR else "🔴"
        table.append(
            f"{mark} {link_label(link['label']).ljust(10)}"
            f"{str(link['joined']).rjust(4)}  {f'{kept}%'.rjust(4)}"
            f"  {pct(link['fresh'], link['joined']).rjust(4)}"
        )
    if len(links) > MAX_LINK_ROWS:
        table.append(_("…and {n} more").format(n=len(links) - MAX_LINK_ROWS))

    body = "\n".join(lines) + "\n\n<pre>" + html.escape("\n".join(table)) + "</pre>"
    return header + "\n\n" + body


# --- личка ---------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db.ensure_user(message.from_user.id, message.from_user.language_code)
    await message.answer(
        _(
            "👋 I approve join requests to your private channel automatically and show "
            "which invite link brings people who stay.\n\n"
            "Add me to the channel as an administrator with the <b>Add members</b> "
            "right — only the channel owner can do that."
        ),
        reply_markup=await main_menu(message.from_user.id),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery) -> None:
    channels = await db.channels_of(call.from_user.id)
    text = _("Your channels:") if channels else _("No channels connected yet.")
    await show(call, text, await main_menu(call.from_user.id))


@router.callback_query(F.data == "how")
async def cb_how(call: CallbackQuery) -> None:
    await show(
        call,
        _(
            "<b>How to connect a channel</b>\n\n"
            "1. Open your channel → Administrators → Add admin\n"
            "2. Pick me and leave the <b>Add members</b> right enabled\n"
            "3. I will write to you right away — switch auto-approve on there\n\n"
            "⚠️ Only the channel <b>owner</b> can connect it. An administrator is not enough.\n\n"
            "⚠️ Join requests that are already pending stay pending: Telegram gives bots no way "
            "to read them. Approve those once by hand — everything after that is on me."
        ),
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=_("⬅️ Back"), callback_data="menu")]]
        ),
    )


@router.callback_query(F.data == "lang")
async def cb_lang(call: CallbackQuery) -> None:
    await db.set_lang(call.from_user.id, "en" if await db.get_lang(call.from_user.id) == "ru" else "ru")
    await cb_menu(call)


@router.callback_query(F.data.startswith("ch:"))
async def cb_channel(call: CallbackQuery) -> None:
    channel = await owned_channel(call, int(call.data.split(":")[1]))
    if channel is None:
        return
    await show(call, await channel_screen(channel), channel_menu(channel))


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery) -> None:
    chat_id = int(call.data.split(":")[1])
    channel = await owned_channel(call, chat_id)
    if channel is None:
        return

    enabled = not channel["auto_approve"]
    await db.set_auto_approve(chat_id, enabled)
    channel = await db.get_channel(chat_id)
    await call.message.edit_text(
        await channel_screen(channel), reply_markup=channel_menu(channel)
    )
    # Тост без show_alert: тумблер жмут по многу раз в день, модалка «ОК» —
    # лишний тап на каждое нажатие.
    await call.answer(
        _("On. Counting from this second.")
        if enabled
        else _("Off. New requests will just sit there.")
    )


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery) -> None:
    # Старый формат stats:<chat>:<days> без сортировки ещё живёт в сообщениях,
    # которые Telegram показывает пользователю после перезапуска бота.
    parts = call.data.split(":")
    chat_id, days = int(parts[1]), int(parts[2])
    sort = parts[3] if len(parts) > 3 else "r"

    channel = await owned_channel(call, chat_id)
    if channel is None:
        return

    report = await db.stats(chat_id, days, sort)
    await show(
        call,
        format_report(channel["title"] or "?", report, bool(channel["auto_approve"])),
        report_menu(chat_id, days, sort),
    )


# --- канал ---------------------------------------------------------------------

@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_my_status(event: ChatMemberUpdated, bot: Bot) -> None:
    status = event.new_chat_member.status
    actor = event.from_user

    # Разжалование до участника выглядит иначе, чем удаление, а последствия те же:
    # Telegram перестаёт слать и заявки, и отписки. Канал надо гасить в обоих случаях.
    if status != ChatMemberStatus.ADMINISTRATOR:
        owner_id = await db.deactivate_channel(event.chat.id)
        if owner_id:
            with await in_lang(owner_id):
                text = _(
                    "⚠️ I lost administrator rights in «{title}» and stopped approving "
                    "requests. Add me back as an admin to resume."
                ).format(title=html.escape(event.chat.title or "?"))
            await notify(bot, owner_id, text)
        return

    # Подключить канал может только владелец: у администратора нет полномочий
    # решать за владельца, кто попадает в его аудиторию.
    try:
        member = await bot.get_chat_member(event.chat.id, actor.id)
        is_owner = member.status == ChatMemberStatus.CREATOR
    except TelegramAPIError:
        is_owner = False

    if not is_owner:
        await notify(
            bot,
            actor.id,
            _(
                "❌ Only the <b>owner</b> of «{title}» can connect it — being an administrator "
                "is not enough. I have left the channel; ask the owner to add me."
            ).format(title=html.escape(event.chat.title or "?")),
        )
        # Выходим, иначе владелец уже не сможет подключить канал: повторное
        # добавление уже добавленного бота нового апдейта не породит.
        try:
            await bot.leave_chat(event.chat.id)
        except TelegramAPIError as exc:
            logging.warning("не смог выйти из %s: %s", event.chat.id, exc)
        return

    known = await db.get_channel(event.chat.id)
    await db.ensure_user(actor.id, actor.language_code)
    await db.upsert_channel(event.chat.id, actor.id, event.chat.title)

    if not event.new_chat_member.can_invite_users:
        await notify(
            bot,
            actor.id,
            _(
                "⚠️ «{title}» is connected, but I have no <b>Add members</b> right — without it "
                "Telegram does not even send me the join requests. Grant it in the channel's "
                "admin settings."
            ).format(title=html.escape(event.chat.title or "?")),
        )
        return

    # Владелец правит галки админа — апдейт прилетает каждый раз. Поздравлять
    # с подключением второй раз незачем.
    if known is not None and known["active"]:
        return

    await notify(
        bot,
        actor.id,
        _("✅ «{title}» is connected. Switch auto-approve on:").format(
            title=html.escape(event.chat.title or "?")
        ),
        reply_markup=channel_menu(await db.get_channel(event.chat.id)),
    )


# Заявка уже удовлетворена: апдейт пришёл повторно после рестарта или её принял
# руками другой админ. Человек в канале — значит для статистики это «принято».
_ALREADY_IN = ("USER_ALREADY_PARTICIPANT", "HIDE_REQUESTER_MISSING")
_CHANNEL_GONE = ("chat not found", "bot is not a member", "CHAT_WRITE_FORBIDDEN")


async def approve(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    """True — принято, False — не удалось, None — канал для бота недоступен."""
    for _attempt in range(3):
        try:
            await bot.approve_chat_join_request(chat_id, user_id)
            return True
        except TelegramRetryAfter as exc:
            # Под закупом заявки идут пачкой и Telegram отвечает 429. Без паузы
            # и повтора заявка просто теряется: очередь ботам недоступна.
            logging.info("429 на %s, ждём %s c", chat_id, exc.retry_after)
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            return None
        except TelegramBadRequest as exc:
            if any(marker in exc.message for marker in _ALREADY_IN):
                return True
            if any(marker in exc.message for marker in _CHANNEL_GONE):
                return None
            logging.warning("не принял заявку в %s: %s", chat_id, exc)
            return False
        except TelegramAPIError as exc:
            logging.warning("не принял заявку в %s: %s", chat_id, exc)
            return False
    return False


@router.chat_join_request()
async def on_join_request(event: ChatJoinRequest, bot: Bot) -> None:
    channel = await db.get_channel(event.chat.id)
    if channel is None or not channel["active"] or not channel["auto_approve"]:
        return

    user = event.from_user
    result = await approve(bot, event.chat.id, user.id)

    if result is None:
        owner_id = await db.deactivate_channel(event.chat.id)
        if owner_id:
            with await in_lang(owner_id):
                text = _(
                    "⚠️ I can no longer access «{title}» and stopped approving requests."
                ).format(title=html.escape(event.chat.title or "?"))
            await notify(bot, owner_id, text)
        return

    link = event.invite_link
    await db.record_join(
        event.chat.id,
        user.id,
        link.invite_link if link else None,
        link.name if link else None,
        user.language_code,
        bool(user.is_premium),
        result,
        # Время подачи, а не обработки: по нему повторный апдейт узнаётся как тот же.
        int(event.date.timestamp()),
    )


@router.chat_member(F.chat.type == ChatType.CHANNEL)
async def on_member_left(event: ChatMemberUpdated) -> None:
    if event.new_chat_member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return
    # Тумблер решает, принимать ли заявки, а не считать ли отписки: иначе за
    # выключенную на ночь автоприёмку удержание нарисуется стопроцентным.
    channel = await db.get_channel(event.chat.id)
    if channel is not None and channel["active"]:
        await db.record_leave(event.chat.id, event.new_chat_member.user.id)


# --- запуск --------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    await db.connect()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    i18n = I18n(path=config.LOCALES_DIR, default_locale=config.DEFAULT_LOCALE, domain="messages")
    DBI18nMiddleware(i18n).setup(dispatcher)

    try:
        # resolve_used_update_types обязателен: chat_member Telegram по умолчанию
        # не присылает, а без него не посчитать отписки.
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
