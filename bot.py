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
from aiogram.exceptions import TelegramAPIError
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

STATS_PERIODS = (7, 30)


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

async def main_menu(owner_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if channel["auto_approve"] else "⬜️ ") + (channel["title"] or "?"),
                callback_data=f"ch:{channel['chat_id']}",
            )
        ]
        for channel in await db.channels_of(owner_id)
    ]
    rows.append([InlineKeyboardButton(text=_("➕ Connect a channel"), callback_data="how")])
    rows.append([InlineKeyboardButton(text="🌐 Русский / English", callback_data="lang")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_menu(chat_id: int, auto_approve: bool) -> InlineKeyboardMarkup:
    toggle = _("🔴 Turn auto-approve OFF") if auto_approve else _("🟢 Turn auto-approve ON")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle, callback_data=f"toggle:{chat_id}")],
            *[
                [
                    InlineKeyboardButton(
                        text=_("📊 Stats, {days} days").format(days=days),
                        callback_data=f"stats:{chat_id}:{days}",
                    )
                ]
                for days in STATS_PERIODS
            ],
            [InlineKeyboardButton(text=_("⬅️ Back"), callback_data="menu")],
        ]
    )


# --- вспомогательное -----------------------------------------------------------

async def notify(bot: Bot, user_id: int, text: str) -> None:
    """Написать владельцу. Молча пропускаем, если он не запускал бота."""
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError as exc:
        logging.info("не доставлено %s: %s", user_id, exc)


def format_stats(title: str, report: dict) -> str:
    totals, links = report["totals"], report["links"]
    approved = totals["approved"]

    def pct(part: int, whole: int) -> str:
        return f"{round(100 * part / whole)}%" if whole else "—"

    head = [
        _("Requests").ljust(12) + str(totals["requests"]),
        _("Approved").ljust(12) + str(approved),
        _("Left").ljust(12) + f"{totals['gone']}  ({pct(totals['gone'], approved)})",
        "",
        _("Premium").ljust(12) + pct(totals["premium"], totals["requests"]),
        _("New accts").ljust(12) + pct(totals["fresh"], totals["requests"]),
    ]

    if links:
        head += ["", _("By invite link:")]
        for link in links:
            stayed = link["joined"] - link["gone"]
            label = link["label"]
            label = label if len(label) <= 14 else label[:13] + "…"
            head.append(
                f"  {label.ljust(15)}{str(link['joined']).rjust(4)} → "
                f"{str(stayed).rjust(4)}  {pct(stayed, link['joined']).rjust(4)}"
            )

    body = html.escape("\n".join(head))
    header = _("📊 {title} · {days} days").format(title=html.escape(title), days=report["days"])
    return f"<b>{header}</b>\n\n<pre>{body}</pre>"


# --- личка ---------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db.ensure_user(message.from_user.id, message.from_user.language_code)
    await message.answer(
        _(
            "👋 I approve join requests to your private channel automatically.\n\n"
            "To connect a channel, add me as an administrator with the "
            "<b>Add members</b> right. Only the channel owner can do this."
        ),
        reply_markup=await main_menu(message.from_user.id),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery) -> None:
    await call.message.edit_text(
        _("Your channels:"), reply_markup=await main_menu(call.from_user.id)
    )
    await call.answer()


@router.callback_query(F.data == "how")
async def cb_how(call: CallbackQuery) -> None:
    await call.message.edit_text(
        _(
            "<b>How to connect a channel</b>\n\n"
            "1. Open your channel → Administrators → Add admin\n"
            "2. Pick me and leave the <b>Add members</b> right enabled\n"
            "3. Come back here — the channel will show up in the list\n\n"
            "⚠️ Only the channel <b>owner</b> can connect it. An administrator is not enough.\n\n"
            "⚠️ Join requests that are already pending stay pending: Telegram gives bots no way "
            "to read them. Approve those once by hand — everything after that is on me."
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=_("⬅️ Back"), callback_data="menu")]]
        ),
    )
    await call.answer()


@router.callback_query(F.data == "lang")
async def cb_lang(call: CallbackQuery) -> None:
    await db.set_lang(call.from_user.id, "en" if await db.get_lang(call.from_user.id) == "ru" else "ru")
    await cb_menu(call)


@router.callback_query(F.data.startswith("ch:"))
async def cb_channel(call: CallbackQuery) -> None:
    chat_id = int(call.data.split(":")[1])
    channel = await db.get_channel(chat_id)
    if channel is None or channel["owner_id"] != call.from_user.id:
        await call.answer(_("Channel not found."), show_alert=True)
        return
    await call.message.edit_text(
        f"<b>{html.escape(channel['title'] or '?')}</b>",
        reply_markup=channel_menu(chat_id, bool(channel["auto_approve"])),
    )
    await call.answer()


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery) -> None:
    chat_id = int(call.data.split(":")[1])
    channel = await db.get_channel(chat_id)
    if channel is None or channel["owner_id"] != call.from_user.id:
        await call.answer(_("Channel not found."), show_alert=True)
        return

    enabled = not channel["auto_approve"]
    await db.set_auto_approve(chat_id, enabled)
    await call.message.edit_reply_markup(reply_markup=channel_menu(chat_id, enabled))
    await call.answer(
        # Статистика пишется только при включённом тумблере, поэтому отчёт
        # заполняется с этой секунды, а не задним числом.
        _("Auto-approve is ON. Stats start collecting from now.")
        if enabled
        else _("Auto-approve is OFF. Requests are no longer approved or counted."),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery) -> None:
    _prefix, raw_chat_id, raw_days = call.data.split(":")
    chat_id, days = int(raw_chat_id), int(raw_days)
    channel = await db.get_channel(chat_id)
    if channel is None or channel["owner_id"] != call.from_user.id:
        await call.answer(_("Channel not found."), show_alert=True)
        return

    report = await db.stats(chat_id, days)
    await call.message.edit_text(
        format_stats(channel["title"] or "?", report),
        reply_markup=channel_menu(chat_id, bool(channel["auto_approve"])),
    )
    await call.answer()


# --- канал ---------------------------------------------------------------------

@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_my_status(event: ChatMemberUpdated, bot: Bot) -> None:
    status = event.new_chat_member.status
    actor = event.from_user

    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        owner_id = await db.deactivate_channel(event.chat.id)
        if owner_id:
            await notify(
                bot,
                owner_id,
                _("⚠️ I was removed from «{title}» and stopped approving requests.").format(
                    title=html.escape(event.chat.title or "?")
                ),
            )
        return

    if status != ChatMemberStatus.ADMINISTRATOR:
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

    await notify(
        bot,
        actor.id,
        _("✅ «{title}» is connected. Send /start and switch auto-approve on.").format(
            title=html.escape(event.chat.title or "?")
        ),
    )


@router.chat_join_request()
async def on_join_request(event: ChatJoinRequest, bot: Bot) -> None:
    channel = await db.get_channel(event.chat.id)
    if channel is None or not channel["active"] or not channel["auto_approve"]:
        return

    user = event.from_user
    try:
        await bot.approve_chat_join_request(event.chat.id, user.id)
        approved = True
    except TelegramAPIError as exc:
        logging.warning("не принял заявку %s в %s: %s", user.id, event.chat.id, exc)
        approved = False

    link = event.invite_link
    await db.record_join(
        event.chat.id,
        user.id,
        link.invite_link if link else None,
        link.name if link else None,
        user.language_code,
        bool(user.is_premium),
        approved,
    )


@router.chat_member(F.chat.type == ChatType.CHANNEL)
async def on_member_left(event: ChatMemberUpdated) -> None:
    if event.new_chat_member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return
    channel = await db.get_channel(event.chat.id)
    if channel is not None and channel["auto_approve"]:
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
