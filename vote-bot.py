# /// script
# requires-python = ">=3.11"
# dependencies = ["python-telegram-bot[job-queue]>=22,<23", "python-dotenv>=1,<2", "croniter>=6,<7"]
# ///

import asyncio
import logging
import os
from dataclasses import replace
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginChannel, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from config import CrontabTrigger, DAY_NAMES, Settings, SettingsStore


LOGGER = logging.getLogger(__name__)
PROMPTS = {
    "question": "Send the question. Variables: {date}, {weekday}, {{day_of_week}}, {time}.",
    "options": "Send 2 to 10 answer options, one per line.",
    "schedule": "Send weekdays and time (mon tue wed, 14-31), or cron: 0 12 * * 1,4. Interval schedules (every 2 minutes) also work.",
    "timezone": "Send an IANA time zone, for example: Europe/Warsaw",
    "group": "Send the destination chat ID (a negative number), or forward a channel post here. Use /chatid in a group to get its ID.",
}


def edit_setting(settings: Settings, field: str, value: str) -> Settings:
    if field == "question":
        updated = replace(settings, question=value)
    elif field == "options":
        updated = replace(settings, options=tuple(option.strip() for option in value.splitlines()))
    elif field == "schedule":
        if value.strip().lower().startswith("cron:"):
            expression = " ".join(value.strip()[5:].split())
            updated = replace(settings, cron_expression=expression, interval_minutes=None)
            updated.validate()
            return updated
        parts = value.lower().split()
        if len(parts) == 3 and parts[0] == "every" and parts[2] in {"minute", "minutes"}:
            try:
                updated = replace(settings, interval_minutes=int(parts[1]), cron_expression=None)
            except ValueError as error:
                raise ValueError("Use an interval such as: every 2 minutes") from error
            updated.validate()
            return updated
        try:
            weekdays, clock = value.lower().rsplit(maxsplit=1)
            days = tuple(sorted({DAY_NAMES.index(day) for day in weekdays.replace(",", " ").split()}))
            hour, minute = map(int, clock.replace("-", ":").split(":"))
        except ValueError as error:
            raise ValueError("Use mon tue wed, 14-31 or cron: 0 12 * * 1,4.") from error
        updated = replace(settings, days=days, hour=hour, minute=minute, interval_minutes=None, cron_expression=None)
    elif field == "timezone":
        updated = replace(settings, timezone=value.strip())
    elif field == "group":
        try:
            chat_id = int(value)
        except ValueError as error:
            raise ValueError("Send a negative numeric group or channel ID.") from error
        if chat_id >= 0:
            raise ValueError("A group or channel ID must be negative.")
        updated = replace(settings, chat_id=chat_id)
    else:
        raise ValueError("Unknown setting.")
    updated.validate()
    return updated


class PollBot:
    def __init__(self, store: SettingsStore, admin_ids: set[int]):
        self.store = store
        self.settings = store.load()
        self.admin_ids = admin_ids
        self.send_lock = asyncio.Lock()

    def authorized(self, update: Update, private: bool = True) -> bool:
        return bool(
            update.effective_user and update.effective_user.id in self.admin_ids
            and update.effective_chat
            and (not private or update.effective_chat.type == "private")
        )

    def panel(self) -> tuple[str, InlineKeyboardMarkup]:
        settings = self.settings
        days = ",".join(DAY_NAMES[day] for day in settings.days)
        schedule = (
            f"Every {settings.interval_minutes} minutes"
            if settings.interval_minutes is not None
            else f"{days} {settings.hour:02d}:{settings.minute:02d}"
        )
        if settings.cron_expression is not None:
            schedule = f"cron: {settings.cron_expression}"
        text = (
            f"Poll settings\n\nQuestion: {settings.question}\n"
            f"Answers: {' / '.join(settings.options)}\n"
            f"Schedule: {schedule}\n"
            f"Time zone: {settings.timezone}\nDestination: {settings.chat_id or 'Not set'}\n"
            f"Posting: {'Enabled' if settings.enabled else 'Disabled'}"
        )
        rows = [
            [InlineKeyboardButton("Question", callback_data="question"), InlineKeyboardButton("Answers", callback_data="options")],
            [InlineKeyboardButton("Schedule", callback_data="schedule"), InlineKeyboardButton("Time zone", callback_data="timezone")],
            [InlineKeyboardButton("Destination", callback_data="group")],
            [InlineKeyboardButton(f"Anonymous: {'On' if settings.anonymous else 'Off'}", callback_data="anonymous")],
            [InlineKeyboardButton(f"Multiple answers: {'On' if settings.multiple else 'Off'}", callback_data="multiple")],
            [InlineKeyboardButton("Disable" if settings.enabled else "Enable", callback_data="enabled"), InlineKeyboardButton("Preview", callback_data="preview")],
            [InlineKeyboardButton("Send now...", callback_data="sendnow")],
        ]
        return text, InlineKeyboardMarkup(rows)

    async def admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        context.user_data.pop("editing", None)
        text, keyboard = self.panel()
        await update.effective_message.reply_text(text, reply_markup=keyboard)

    async def chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update, private=False):
            await update.effective_message.reply_text(f"Chat ID: {update.effective_chat.id}")

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.authorized(update):
            context.user_data.pop("editing", None)
            await self.admin(update, context)

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.authorized(update):
            await query.answer("Not authorized.", show_alert=True)
            return
        await query.answer()
        action = query.data
        context.user_data.pop("editing", None)
        if action in PROMPTS:
            context.user_data["editing"] = action
            await update.effective_message.reply_text(PROMPTS[action] + "\n/cancel to cancel.")
        elif action in {"anonymous", "multiple", "enabled"}:
            try:
                self.save(replace(self.settings, **{action: not getattr(self.settings, action)}), context.application)
            except ValueError as error:
                await update.effective_message.reply_text(str(error))
                return
            await self.admin(update, context)
        elif action == "preview":
            await self.send_with_feedback(update, context, preview=True)
        elif action == "sendnow":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Send to group now", callback_data="confirm_send"),
                InlineKeyboardButton("Cancel", callback_data="cancel"),
            ]])
            await update.effective_message.reply_text("Send one poll to the configured group?", reply_markup=keyboard)
        elif action == "confirm_send":
            await query.edit_message_reply_markup(reply_markup=None)
            await self.send_with_feedback(update, context)
        elif action == "cancel":
            await query.edit_message_reply_markup(reply_markup=None)
            await self.admin(update, context)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        field = context.user_data.get("editing")
        if not field:
            await self.admin(update, context)
            return
        try:
            updated = edit_setting(self.settings, field, update.effective_message.text)
            self.save(updated, context.application)
        except ValueError as error:
            await update.effective_message.reply_text(f"Not saved: {error}\nTry again or /cancel.")
            return
        context.user_data.pop("editing", None)
        await self.admin(update, context)

    async def forwarded_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        origin = update.effective_message.forward_origin
        if not isinstance(origin, MessageOriginChannel):
            await update.effective_message.reply_text("Forward an original channel post with its source visible.")
            return
        try:
            member = await context.bot.get_chat_member(origin.chat.id, context.bot.id)
            if member.status != "administrator" or not getattr(member, "can_post_messages", False):
                await update.effective_message.reply_text("Make the bot a channel administrator with permission to post messages, then forward the post again.")
                return
            self.save(replace(self.settings, chat_id=origin.chat.id, anonymous=True, enabled=False), context.application)
        except TelegramError:
            await update.effective_message.reply_text("Could not verify channel access. Check bot membership and administrator permissions.")
            return
        context.user_data.pop("editing", None)
        await update.effective_message.reply_text(f"Destination set to {origin.chat.title}. Channel polls are anonymous. Posting is disabled until you enable it.")
        await self.admin(update, context)

    def save(self, settings: Settings, application: Application) -> None:
        self.store.save(settings)
        self.settings = settings
        self.reschedule(application)

    def reschedule(self, application: Application) -> None:
        queue = application.job_queue
        if queue is None:
            raise RuntimeError("Install python-telegram-bot with the job-queue extra.")
        for job in queue.get_jobs_by_name("scheduled_poll"):
            job.schedule_removal()
        settings = self.settings
        if settings.enabled:
            if settings.cron_expression is not None:
                queue.run_custom(
                    self.scheduled_poll, name="scheduled_poll",
                    job_kwargs={
                        "trigger": CrontabTrigger(settings.cron_expression, settings.timezone),
                        "coalesce": True, "max_instances": 1, "misfire_grace_time": 60,
                    },
                )
                return
            if settings.interval_minutes is not None:
                interval = timedelta(minutes=settings.interval_minutes)
                queue.run_repeating(
                    self.scheduled_poll, interval=interval, first=interval,
                    name="scheduled_poll",
                    job_kwargs={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
                )
                return
            queue.run_daily(
                self.scheduled_poll,
                time=time(settings.hour, settings.minute, tzinfo=ZoneInfo(settings.timezone)),
                days=settings.scheduler_days,
                name="scheduled_poll",
                job_kwargs={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
            )

    async def startup(self, application: Application) -> None:
        self.reschedule(application)
        LOGGER.info("Bot started. Scheduled posting is %s.", "enabled" if self.settings.enabled else "disabled")

    async def send_poll(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None) -> None:
        async with self.send_lock:
            settings = self.settings
            destination = chat_id if chat_id is not None else settings.chat_id
            if destination is None:
                raise ValueError("Set the destination group first.")
            await context.bot.send_poll(
                chat_id=destination, question=settings.render(), options=settings.options,
                is_anonymous=settings.anonymous, allows_multiple_answers=settings.multiple,
            )

    async def send_with_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, preview: bool = False) -> None:
        try:
            await self.send_poll(context, update.effective_chat.id if preview else None)
            if not preview:
                await update.effective_message.reply_text("Poll sent.")
        except (TelegramError, ValueError):
            await update.effective_message.reply_text(
                "Poll was not confirmed. Check the group ID, bot membership and poll permissions. "
                "Check the group before retrying; a timeout may still have delivered the poll."
            )

    async def scheduled_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.settings.enabled:
            return
        try:
            await self.send_poll(context)
        except (TelegramError, ValueError) as error:
            LOGGER.error("Scheduled poll failed (%s).", type(error).__name__)
            for admin_id in self.admin_ids:
                try:
                    await context.bot.send_message(admin_id, "Scheduled poll was not confirmed. Check the group and bot permissions before retrying.")
                except TelegramError:
                    LOGGER.warning("Could not notify admin %s.", admin_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Telegram update failed (%s).", type(context.error).__name__)


def main() -> None:
    base = Path(__file__).resolve().parent
    load_dotenv(base / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        admins = {int(value.strip()) for value in os.getenv("ADMIN_USER_IDS", "").split(",") if value.strip()}
    except ValueError:
        raise SystemExit("ADMIN_USER_IDS must be comma-separated numeric Telegram user IDs.") from None
    if not token or not admins or any(admin <= 0 for admin in admins):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and ADMIN_USER_IDS in .env before starting.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = PollBot(SettingsStore(base / "data" / "settings.db"), admins)
    application = Application.builder().token(token).post_init(bot.startup).build()
    application.add_handler(CommandHandler(["start", "admin"], bot.admin))
    application.add_handler(CommandHandler("chatid", bot.chatid))
    application.add_handler(CommandHandler("cancel", bot.cancel))
    application.add_handler(CallbackQueryHandler(bot.button))
    application.add_handler(MessageHandler(filters.FORWARDED, bot.forwarded_channel))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.text))
    application.add_error_handler(on_error)
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
