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
    "name": "Send a name for this poll (1 to 60 characters).",
    "destination_label": "Send a label for this destination (1 to 60 characters).",
    "question": "Send the question. Variables: {date}, {weekday}, {{day_of_week}}, {time}.",
    "options": "Send 2 to 10 answer options, one per line.",
    "schedule": "Send weekdays and time (mon tue wed, 14-31), or cron: 0 12 * * 1,4. Interval schedules (every 2 minutes) also work.",
    "timezone": "Send an IANA time zone, for example: Europe/Warsaw",
    "group": "Send the destination chat ID (a negative number), or forward a channel post here. Use /chatid in a group to get its ID.",
}


def edit_setting(settings: Settings, field: str, value: str) -> Settings:
    if field == "name":
        updated = replace(settings, name=value.strip())
    elif field == "destination_label":
        if settings.chat_id is None or not value.strip():
            raise ValueError("Set a destination first, then enter a nonempty label.")
        updated = replace(settings, destination_label=value.strip())
    elif field == "question":
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
        updated = replace(settings, chat_id=chat_id, destination_label="", is_channel=False, enabled=False)
    else:
        raise ValueError("Unknown setting.")
    updated.validate()
    return updated


class PollBot:
    def __init__(self, store: SettingsStore, admin_ids: set[int]):
        self.store = store
        self.polls = store.list_polls()
        self.admin_ids = admin_ids
        self.send_lock = asyncio.Lock()

    @property
    def settings(self) -> Settings:
        return self.polls[1]

    @settings.setter
    def settings(self, settings: Settings) -> None:
        self.polls[1] = settings

    def authorized(self, update: Update, private: bool = True) -> bool:
        return bool(
            update.effective_user and update.effective_user.id in self.admin_ids
            and update.effective_chat
            and (not private or update.effective_chat.type == "private")
        )

    def overview(self, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
        items = list(self.polls.items())
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        rows = []
        for poll_id, settings in items[page * 8:(page + 1) * 8]:
            destination = settings.destination_label or ("Unnamed destination" if settings.chat_id else "No destination")
            label = f"{'On' if settings.enabled else 'Off'}: {settings.name[:26]} > {destination[:26]}"
            rows.append([InlineKeyboardButton(label, callback_data=f"open:{poll_id}")])
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("Previous", callback_data=f"list:{page - 1}"))
        if (page + 1) * 8 < len(items):
            navigation.append(InlineKeyboardButton("Next", callback_data=f"list:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([InlineKeyboardButton("New poll", callback_data="new:0")])
        return f"Polls\n\n{len(items)} configured", InlineKeyboardMarkup(rows)

    def panel(self, poll_id: int = 1) -> tuple[str, InlineKeyboardMarkup]:
        settings = self.polls[poll_id]
        days = ",".join(DAY_NAMES[day] for day in settings.days)
        schedule = (
            f"Every {settings.interval_minutes} minutes"
            if settings.interval_minutes is not None
            else f"{days} {settings.hour:02d}:{settings.minute:02d}"
        )
        if settings.cron_expression is not None:
            schedule = f"cron: {settings.cron_expression}"
        text = (
            f"Poll: {settings.name}\n\nQuestion: {settings.question}\n"
            f"Answers: {' / '.join(settings.options)}\n"
            f"Schedule: {schedule}\n"
            f"Time zone: {settings.timezone}\nDestination: {settings.destination_label or ('Unnamed destination' if settings.chat_id else 'Not set')}\n"
            f"Posting: {'Enabled' if settings.enabled else 'Disabled'}"
        )
        rows = [
            [InlineKeyboardButton("Name", callback_data="name")],
            [InlineKeyboardButton("Question", callback_data="question"), InlineKeyboardButton("Answers", callback_data="options")],
            [InlineKeyboardButton("Schedule", callback_data="schedule"), InlineKeyboardButton("Time zone", callback_data="timezone")],
            [InlineKeyboardButton("Destination", callback_data="destination")],
            [InlineKeyboardButton(f"Anonymous: {'On' if settings.anonymous else 'Off'}", callback_data="anonymous")],
            [InlineKeyboardButton(f"Multiple answers: {'On' if settings.multiple else 'Off'}", callback_data="multiple")],
            [InlineKeyboardButton("Disable" if settings.enabled else "Enable", callback_data="enabled"), InlineKeyboardButton("Preview", callback_data="preview")],
            [InlineKeyboardButton("Send now...", callback_data="sendnow")],
            [InlineKeyboardButton("Delete poll...", callback_data="delete")],
        ]
        rows = [[InlineKeyboardButton(button.text, callback_data=f"{button.callback_data}:{poll_id}") for button in row] for row in rows]
        rows.append([InlineKeyboardButton("All polls", callback_data="list:0")])
        return text, InlineKeyboardMarkup(rows)

    async def show_poll(self, update: Update, context: ContextTypes.DEFAULT_TYPE, poll_id: int) -> None:
        if poll_id not in self.polls:
            await update.effective_message.reply_text("This poll no longer exists.")
            await self.admin(update, context)
            return
        context.user_data["poll_id"] = poll_id
        context.user_data.pop("editing", None)
        context.user_data.pop("editing_poll_id", None)
        text, keyboard = self.panel(poll_id)
        await update.effective_message.reply_text(text, reply_markup=keyboard)

    async def destinations(self, update: Update, context: ContextTypes.DEFAULT_TYPE, poll_id: int, page: int = 0) -> None:
        context.user_data["poll_id"] = poll_id
        sources = {}
        for source_id, settings in self.polls.items():
            if settings.chat_id is not None:
                sources.setdefault(settings.chat_id, (source_id, settings))
        items = list(sources.values())
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        rows = [[InlineKeyboardButton(
            settings.destination_label or f"Destination of {settings.name}",
            callback_data=f"dest:{poll_id}:{source_id}",
        )] for source_id, settings in items[page * 8:(page + 1) * 8]]
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton("Previous", callback_data=f"destinations:{poll_id}:{page - 1}"))
        if (page + 1) * 8 < len(items):
            navigation.append(InlineKeyboardButton("Next", callback_data=f"destinations:{poll_id}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.extend([
            [InlineKeyboardButton("Enter chat ID", callback_data=f"group:{poll_id}")],
            [InlineKeyboardButton("Edit label", callback_data=f"destination_label:{poll_id}")],
            [InlineKeyboardButton("Back to poll", callback_data=f"open:{poll_id}")],
        ])
        await update.effective_message.reply_text("Destinations", reply_markup=InlineKeyboardMarkup(rows))

    async def admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        context.user_data.pop("editing", None)
        context.user_data.pop("editing_poll_id", None)
        context.user_data.pop("poll_id", None)
        text, keyboard = self.overview()
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
        parts = (query.data or "").split(":")
        try:
            action, poll_id = parts[0], int(parts[1])
        except (IndexError, ValueError):
            await update.effective_message.reply_text("Open /admin and select a poll to use the updated panel.")
            return
        context.user_data.pop("editing", None)
        context.user_data.pop("editing_poll_id", None)
        if action == "list":
            context.user_data.pop("poll_id", None)
            text, keyboard = self.overview(poll_id)
            await update.effective_message.reply_text(text, reply_markup=keyboard)
            return
        if action == "new":
            settings = Settings(name=f"Poll {max(self.polls, default=0) + 1}")
            poll_id = self.store.create(settings)
            self.polls[poll_id] = settings
            await self.show_poll(update, context, poll_id)
            context.user_data["editing"] = "name"
            context.user_data["editing_poll_id"] = poll_id
            await update.effective_message.reply_text(PROMPTS["name"])
            return
        if poll_id not in self.polls:
            await self.show_poll(update, context, poll_id)
            return
        context.user_data["poll_id"] = poll_id
        settings = self.polls[poll_id]
        if action == "open" or action == "cancel":
            if action == "cancel":
                await query.edit_message_reply_markup(reply_markup=None)
            await self.show_poll(update, context, poll_id)
        elif action == "destinations" or action == "destination":
            page = int(parts[2]) if len(parts) == 3 else 0
            await self.destinations(update, context, poll_id, page)
        elif action == "dest":
            source = self.polls.get(int(parts[2])) if len(parts) == 3 else None
            if source is None or source.chat_id is None:
                await update.effective_message.reply_text("This destination is no longer available.")
                return
            self.save(replace(
                settings, chat_id=source.chat_id, destination_label=source.destination_label,
                is_channel=source.is_channel, anonymous=True if source.is_channel else settings.anonymous, enabled=False,
            ), context.application, poll_id)
            await self.show_poll(update, context, poll_id)
        elif action == "delete":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Delete this poll", callback_data=f"confirm_delete:{poll_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{poll_id}"),
            ]])
            await update.effective_message.reply_text(f"Delete {settings.name}?", reply_markup=keyboard)
        elif action == "confirm_delete":
            await query.edit_message_reply_markup(reply_markup=None)
            self.store.delete(poll_id)
            self.polls.pop(poll_id, None)
            self.reschedule(context.application, poll_id)
            await self.admin(update, context)
        elif action in PROMPTS:
            context.user_data["editing"] = action
            context.user_data["editing_poll_id"] = poll_id
            await update.effective_message.reply_text(PROMPTS[action] + "\n/cancel to cancel.")
        elif action in {"anonymous", "multiple", "enabled"}:
            try:
                self.save(replace(settings, **{action: not getattr(settings, action)}), context.application, poll_id)
            except ValueError as error:
                await update.effective_message.reply_text(str(error))
                return
            await self.show_poll(update, context, poll_id)
        elif action == "preview":
            await self.send_with_feedback(update, context, preview=True, poll_id=poll_id)
        elif action == "sendnow":
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("Send now", callback_data=f"confirm_send:{poll_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{poll_id}"),
            ]])
            await update.effective_message.reply_text(f"Send {settings.name} to its destination now?", reply_markup=keyboard)
        elif action == "confirm_send":
            await query.edit_message_reply_markup(reply_markup=None)
            await self.send_with_feedback(update, context, poll_id=poll_id)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        field = context.user_data.get("editing")
        if not field:
            await self.admin(update, context)
            return
        poll_id = context.user_data.get("editing_poll_id")
        if poll_id not in self.polls:
            await update.effective_message.reply_text("This poll no longer exists.")
            await self.admin(update, context)
            return
        try:
            updated = edit_setting(self.polls[poll_id], field, update.effective_message.text)
            if field == "group":
                chat = await context.bot.get_chat(updated.chat_id)
                if poll_id not in self.polls:
                    raise ValueError("This poll no longer exists.")
                updated = edit_setting(self.polls[poll_id], field, update.effective_message.text)
                updated = replace(
                    updated, destination_label=(chat.title or chat.username or "Destination")[:60],
                    is_channel=chat.type == "channel", anonymous=True if chat.type == "channel" else updated.anonymous,
                )
            self.save(updated, context.application, poll_id)
        except TelegramError:
            await update.effective_message.reply_text("Could not access this destination. Check the ID and bot membership, then try again or /cancel.")
            return
        except ValueError as error:
            await update.effective_message.reply_text(f"Not saved: {error}\nTry again or /cancel.")
            return
        context.user_data.pop("editing", None)
        await self.show_poll(update, context, poll_id)

    async def forwarded_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.authorized(update):
            return
        poll_id = context.user_data.get("poll_id")
        if poll_id is None and len(self.polls) == 1:
            poll_id = next(iter(self.polls))
        if poll_id not in self.polls:
            await update.effective_message.reply_text("Select a poll in /admin before forwarding a channel post.")
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
            if poll_id not in self.polls:
                await update.effective_message.reply_text("This poll no longer exists.")
                return
            self.save(replace(
                self.polls[poll_id], chat_id=origin.chat.id,
                destination_label=(origin.chat.title or "Channel")[:60],
                is_channel=True, anonymous=True, enabled=False,
            ), context.application, poll_id)
        except TelegramError:
            await update.effective_message.reply_text("Could not verify channel access. Check bot membership and administrator permissions.")
            return
        context.user_data.pop("editing", None)
        await update.effective_message.reply_text(f"Destination set to {origin.chat.title}. Channel polls are anonymous. Posting is disabled until you enable it.")
        await self.show_poll(update, context, poll_id)

    def save(self, settings: Settings, application: Application, poll_id: int = 1) -> None:
        self.store.save(settings, poll_id)
        self.polls[poll_id] = settings
        self.reschedule(application, poll_id)

    def reschedule(self, application: Application, poll_id: int | None = None) -> None:
        queue = application.job_queue
        if queue is None:
            raise RuntimeError("Install python-telegram-bot with the job-queue extra.")
        poll_ids = list(self.polls) if poll_id is None else [poll_id]
        for current_id in poll_ids:
            name = f"scheduled_poll:{current_id}"
            for job in queue.get_jobs_by_name(name):
                job.schedule_removal()
            settings = self.polls.get(current_id)
            if settings is None or not settings.enabled:
                continue
            if settings.cron_expression is not None:
                queue.run_custom(
                    self.scheduled_poll, name=name, data=current_id,
                    job_kwargs={
                        "trigger": CrontabTrigger(settings.cron_expression, settings.timezone),
                        "coalesce": True, "max_instances": 1, "misfire_grace_time": 60,
                    },
                )
                continue
            if settings.interval_minutes is not None:
                interval = timedelta(minutes=settings.interval_minutes)
                queue.run_repeating(
                    self.scheduled_poll, interval=interval, first=interval,
                    name=name, data=current_id,
                    job_kwargs={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
                )
                continue
            queue.run_daily(
                self.scheduled_poll,
                time=time(settings.hour, settings.minute, tzinfo=ZoneInfo(settings.timezone)),
                days=settings.scheduler_days,
                name=name, data=current_id,
                job_kwargs={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
            )

    async def startup(self, application: Application) -> None:
        self.reschedule(application)
        LOGGER.info("Bot started with %s polls, %s enabled.", len(self.polls), sum(settings.enabled for settings in self.polls.values()))

    async def send_poll(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None, poll_id: int = 1, scheduled: bool = False) -> None:
        async with self.send_lock:
            settings = self.polls.get(poll_id)
            if scheduled and (settings is None or not settings.enabled):
                return
            if settings is None:
                raise ValueError("This poll no longer exists.")
            destination = chat_id if chat_id is not None else settings.chat_id
            if destination is None:
                raise ValueError("Set the destination group first.")
            await context.bot.send_poll(
                chat_id=destination, question=settings.render(), options=settings.options,
                is_anonymous=settings.anonymous, allows_multiple_answers=settings.multiple,
            )

    async def send_with_feedback(self, update: Update, context: ContextTypes.DEFAULT_TYPE, preview: bool = False, poll_id: int = 1) -> None:
        try:
            await self.send_poll(context, update.effective_chat.id if preview else None, poll_id)
            if not preview:
                await update.effective_message.reply_text("Poll sent.")
        except (TelegramError, ValueError):
            await update.effective_message.reply_text(
                "Poll was not confirmed. Check the group ID, bot membership and poll permissions. "
                "Check the group before retrying; a timeout may still have delivered the poll."
            )

    async def scheduled_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        poll_id = context.job.data if getattr(context, "job", None) else 1
        settings = self.polls.get(poll_id)
        if settings is None or not settings.enabled:
            return
        try:
            await self.send_poll(context, poll_id=poll_id, scheduled=True)
        except (TelegramError, ValueError) as error:
            LOGGER.error("Scheduled poll %s failed (%s).", poll_id, type(error).__name__)
            for admin_id in self.admin_ids:
                try:
                    await context.bot.send_message(admin_id, f"Poll '{settings.name}' was not confirmed. Check its destination and bot permissions before retrying.")
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
