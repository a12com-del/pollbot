import asyncio
import importlib.util
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram import Chat
from telegram.error import TelegramError

from config import Settings, SettingsStore


spec = importlib.util.spec_from_file_location("vote_bot", Path(__file__).resolve().parents[1] / "vote-bot.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bot = module.PollBot(SettingsStore(Path(self.directory.name) / "settings.db"), {123})

    def make_update(self, action: str = "", text: str = "", user_id: int = 123):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=user_id, type="private"),
            effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
            callback_query=SimpleNamespace(data=action, answer=AsyncMock(), edit_message_reply_markup=AsyncMock()),
        )

    def make_context(self, user_data=None):
        queue = Mock()
        queue.get_jobs_by_name.return_value = ()
        return SimpleNamespace(
            user_data=user_data if user_data is not None else {}, application=SimpleNamespace(job_queue=queue),
            bot=SimpleNamespace(
                send_poll=AsyncMock(), send_message=AsyncMock(),
                get_chat=AsyncMock(return_value=Chat(-100789, "supergroup", title="Main studio")),
            ),
        )

    def add_poll(self, **values):
        settings = replace(Settings(), **values)
        poll_id = self.bot.store.create(settings)
        self.bot.polls[poll_id] = settings
        return poll_id

    async def test_saved_personal_defaults_create_independent_disabled_poll(self):
        source = replace(
            Settings(), name="Evening class", question="Joining {weekday}?", options=("Yes", "No", "Maybe"),
            chat_id=-100123, destination_label="Studio", message_thread_id=42,
            timezone="Etc/GMT-4", cron_expression="0 18 * * 1,4", anonymous=True, multiple=True, enabled=True,
        )
        self.bot.settings = source
        self.bot.store.save(source)
        context = self.make_context()
        saved = self.make_update("save_defaults:1")
        await self.bot.button(saved, context)
        self.assertIn("My defaults", saved.effective_message.reply_text.call_args.args[0])
        self.assertEqual(self.bot.store.load(), source)
        self.assertFalse(self.bot.store.load_defaults(123).enabled)
        await self.bot.button(self.make_update("new:0"), context)
        poll_id = context.user_data["poll_id"]
        self.assertEqual(self.bot.polls[poll_id], replace(source, name="Poll 2", enabled=False))
        self.assertEqual(self.bot.store.load(), source)
        self.assertEqual(len(self.bot.polls), 2)
        context.application.job_queue.run_custom.assert_not_called()
        self.bot.save(replace(self.bot.polls[poll_id], question="Only this poll?"), context.application, poll_id)
        self.assertEqual(self.bot.store.load_defaults(123).question, "Joining {weekday}?")

    async def test_personal_defaults_survive_restart_and_do_not_affect_other_admins(self):
        self.bot.store.save_defaults(123, replace(Settings(), question="First admin?", interval_minutes=2))
        self.bot.admin_ids.add(456)
        restarted = module.PollBot(self.bot.store, self.bot.admin_ids)
        first_context = self.make_context()
        second_context = self.make_context()
        await restarted.button(self.make_update("new:0"), first_context)
        await restarted.button(self.make_update("new:0", user_id=456), second_context)
        first = restarted.polls[first_context.user_data["poll_id"]]
        second = restarted.polls[second_context.user_data["poll_id"]]
        self.assertEqual(first.question, "First admin?")
        self.assertEqual(first.interval_minutes, 2)
        self.assertEqual(second.question, Settings().question)
        self.assertIsNone(second.interval_minutes)

    async def test_defaults_reset_requires_confirmation_and_only_resets_current_admin(self):
        self.bot.store.save_defaults(123, replace(Settings(), question="Mine?"))
        self.bot.store.save_defaults(456, replace(Settings(), question="Other?"))
        context = self.make_context()
        await self.bot.button(self.make_update("reset_defaults:0"), context)
        self.assertEqual(self.bot.store.load_defaults(123).question, "Mine?")
        await self.bot.button(self.make_update("defaults:0"), context)
        self.assertEqual(self.bot.store.load_defaults(123).question, "Mine?")
        await self.bot.button(self.make_update("confirm_reset_defaults:0"), context)
        self.assertEqual(self.bot.store.load_defaults(123), Settings())
        self.assertEqual(self.bot.store.load_defaults(456).question, "Other?")
        self.assertEqual(self.bot.store.list_polls(), {1: Settings()})

    async def test_channel_defaults_preserve_anonymity_and_work_without_source_poll(self):
        source = replace(Settings(), name="Channel poll", chat_id=-100123, is_channel=True, anonymous=True)
        self.bot.settings = source
        self.bot.store.save(source)
        context = self.make_context()
        await self.bot.button(self.make_update("save_defaults:1"), context)
        self.bot.store.delete(1)
        self.bot.polls.pop(1)
        await self.bot.defaults(self.make_update(), context)
        await self.bot.button(self.make_update("new:0"), context)
        created = self.bot.polls[context.user_data["poll_id"]]
        self.assertTrue(created.is_channel)
        self.assertTrue(created.anonymous)
        self.assertFalse(created.enabled)

    async def test_unauthorized_users_cannot_read_save_or_reset_defaults(self):
        context = self.make_context()
        update = self.make_update(user_id=456)
        await self.bot.defaults(update, context)
        update.effective_message.reply_text.assert_not_called()
        self.bot.store.save_defaults(456, replace(Settings(), question="Saved?"))
        for action in ("defaults:0", "save_defaults:1", "reset_defaults:0", "confirm_reset_defaults:0"):
            await self.bot.button(self.make_update(action, user_id=456), context)
        self.assertEqual(self.bot.store.load_defaults(456).question, "Saved?")
        self.assertEqual(len(self.bot.polls), 1)

    def test_three_schedule_modes_create_independent_jobs(self):
        application = module.Application.builder().token("123:offline-test-token").build()
        self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123, hour=9)
        second = self.add_poll(name="Midday", enabled=True, chat_id=-100456, cron_expression="0 12 * * 1,4")
        third = self.add_poll(name="Interval", enabled=True, chat_id=-100789, interval_minutes=2)
        self.bot.reschedule(application)
        jobs = application.job_queue.jobs()
        self.assertEqual({job.name for job in jobs}, {"scheduled_poll:1", f"scheduled_poll:{second}", f"scheduled_poll:{third}"})
        self.assertEqual({job.data for job in jobs}, {1, second, third})
        other_job = application.job_queue.get_jobs_by_name(f"scheduled_poll:{second}")[0]
        self.bot.save(replace(self.bot.settings, enabled=False), application)
        self.assertEqual(application.job_queue.get_jobs_by_name("scheduled_poll:1"), ())
        self.assertIs(application.job_queue.get_jobs_by_name(f"scheduled_poll:{second}")[0], other_job)

    async def test_scheduled_sends_use_each_polls_question_and_destination(self):
        self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123, question="Morning?")
        second = self.add_poll(name="Evening", enabled=True, chat_id=-100456, question="Evening?", options=("Join", "Skip"))
        context = self.make_context()
        context.job = SimpleNamespace(data=1)
        await self.bot.scheduled_poll(context)
        context.job.data = second
        await self.bot.scheduled_poll(context)
        calls = context.bot.send_poll.call_args_list
        self.assertEqual([(call.kwargs["chat_id"], call.kwargs["question"]) for call in calls], [(-100123, "Morning?"), (-100456, "Evening?")])
        self.assertEqual(calls[1].kwargs["options"], ("Join", "Skip"))
        self.bot.store.delete(second)
        self.bot.polls.pop(second)
        await self.bot.scheduled_poll(context)
        self.assertEqual(context.bot.send_poll.await_count, 2)

    async def test_admin_edits_are_scoped_to_poll_and_user(self):
        second = self.add_poll(name="Evening")
        self.bot.admin_ids.add(456)
        first_context = self.make_context()
        second_context = self.make_context()
        await self.bot.button(self.make_update("question:1"), first_context)
        await self.bot.button(self.make_update(f"question:{second}", user_id=456), second_context)
        await self.bot.text(self.make_update(text="First question?"), first_context)
        await self.bot.text(self.make_update(text="Second question?", user_id=456), second_context)
        self.assertEqual(self.bot.store.load(1).question, "First question?")
        self.assertEqual(self.bot.store.load(second).question, "Second question?")
        text, keyboard = self.bot.panel(second)
        self.assertIn("Evening", text)
        self.assertTrue(all(button.callback_data.endswith(f":{second}") or button.callback_data == "list:0" for row in keyboard.inline_keyboard for button in row))

    async def test_create_and_confirm_delete_poll_through_panel(self):
        context = self.make_context()
        await self.bot.button(self.make_update("new:0"), context)
        poll_id = context.user_data["poll_id"]
        self.assertEqual(len(self.bot.polls), 2)
        self.assertFalse(self.bot.polls[poll_id].enabled)
        self.assertFalse(self.bot.polls[poll_id].anonymous)
        await self.bot.text(self.make_update(text="Evening class"), context)
        self.assertEqual(self.bot.store.load(poll_id).name, "Evening class")
        await self.bot.button(self.make_update(f"delete:{poll_id}"), context)
        self.assertIn(poll_id, self.bot.polls)
        await self.bot.button(self.make_update(f"confirm_delete:{poll_id}"), context)
        self.assertNotIn(poll_id, self.bot.polls)
        self.assertEqual(len(self.bot.store.list_polls()), 1)
        await self.bot.button(self.make_update(f"enabled:{poll_id}"), context)
        self.assertNotIn(poll_id, self.bot.polls)

    async def test_named_destinations_can_be_reused(self):
        self.bot.settings = replace(Settings(), chat_id=-100123, destination_label="Main studio", is_channel=True, anonymous=True)
        self.bot.store.save(self.bot.settings)
        second = self.add_poll(name="Evening", anonymous=False)
        context = self.make_context()
        update = self.make_update(f"destination:{second}")
        await self.bot.button(update, context)
        keyboard = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "Main studio")
        await self.bot.button(self.make_update(f"dest:{second}:1"), context)
        settings = self.bot.store.load(second)
        self.assertEqual((settings.chat_id, settings.destination_label), (-100123, "Main studio"))
        self.assertTrue(settings.anonymous)
        self.assertTrue(settings.is_channel)
        self.assertFalse(settings.enabled)
        with self.assertRaises(ValueError):
            replace(settings, anonymous=False).validate()
        self.assertNotIn("-100123", self.bot.panel(second)[0])

    async def test_numeric_destination_gets_telegram_title_and_can_be_renamed(self):
        second = self.add_poll(name="Evening")
        context = self.make_context()
        await self.bot.button(self.make_update(f"group:{second}"), context)
        await self.bot.text(self.make_update(text="-100789"), context)
        self.assertEqual(self.bot.store.load(second).destination_label, "Main studio")
        await self.bot.button(self.make_update(f"destination_label:{second}"), context)
        await self.bot.text(self.make_update(text="Dance group"), context)
        self.assertEqual(self.bot.store.load(second).destination_label, "Dance group")

    async def test_one_failed_poll_does_not_stop_another(self):
        self.bot.settings = replace(Settings(), name="Morning", chat_id=-100123, enabled=True)
        second = self.add_poll(name="Evening", chat_id=-100456, enabled=True)
        context = self.make_context()
        context.bot.send_poll.side_effect = [TelegramError("Test failure"), None]
        context.job = SimpleNamespace(data=1)
        await self.bot.scheduled_poll(context)
        context.job.data = second
        await self.bot.scheduled_poll(context)
        self.assertEqual(context.bot.send_poll.await_count, 2)
        self.assertIn("Morning", context.bot.send_message.call_args.args[1])

    async def test_queued_poll_is_not_sent_after_disable_or_delete(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123)
                context = self.make_context()
                context.job = SimpleNamespace(data=1)
                await self.bot.send_lock.acquire()
                pending = asyncio.create_task(self.bot.scheduled_poll(context))
                await asyncio.sleep(0)
                if deleted:
                    self.bot.polls.pop(1)
                else:
                    self.bot.settings = replace(self.bot.settings, enabled=False)
                self.bot.send_lock.release()
                await pending
                context.bot.send_poll.assert_not_called()
                context.bot.send_message.assert_not_called()

    async def test_preview_and_confirm_send_remain_bound_to_original_poll(self):
        second = self.add_poll(name="Evening", chat_id=-100456, question="Evening?")
        context = self.make_context({"poll_id": 1})
        await self.bot.button(self.make_update(f"preview:{second}"), context)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["chat_id"], 123)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["question"], "Evening?")
        context.user_data["poll_id"] = 1
        await self.bot.button(self.make_update(f"confirm_send:{second}"), context)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["chat_id"], -100456)

    async def test_unauthorized_callback_cannot_create_or_delete(self):
        context = self.make_context()
        for action in ("new:0", "confirm_delete:1", "enabled:1"):
            await self.bot.button(self.make_update(action, user_id=456), context)
        self.assertEqual(self.bot.store.list_polls(), {1: Settings()})

    def test_poll_and_destination_lists_paginate(self):
        for index in range(10):
            self.add_poll(name=f"Poll {index + 2}", chat_id=-100000 - index, destination_label=f"Group {index + 1}")
        text, keyboard = self.bot.overview()
        self.assertIn("11 configured", text)
        self.assertEqual(len(keyboard.inline_keyboard), 10)
        self.assertEqual(keyboard.inline_keyboard[-2][0].callback_data, "list:1")
        second_page = self.bot.overview(1)[1]
        self.assertEqual(second_page.inline_keyboard[-2][0].callback_data, "list:0")

    def test_authorization_requires_admin_and_private_chat(self):
        for user_id, chat_type, expected in ((123, "private", True), (456, "private", False), (123, "supergroup", False)):
            update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(type=chat_type))
            self.assertEqual(self.bot.authorized(update), expected)

    def test_editing_configurable_fields(self):
        settings = module.edit_setting(Settings(), "schedule", "mon,thu 13:00")
        self.assertEqual(settings.days, (0, 3))
        self.assertEqual((settings.hour, settings.minute), (13, 0))
        settings = module.edit_setting(settings, "timezone", "Europe/Warsaw")
        settings = module.edit_setting(settings, "question", "Joining on {date}?")
        settings = module.edit_setting(settings, "options", "Yes\nNo\nMaybe")
        settings = module.edit_setting(settings, "group", "-100123")
        self.assertEqual(settings.options, ("Yes", "No", "Maybe"))
        self.assertEqual(settings.chat_id, -100123)
        for field, value in (("schedule", "bad"), ("schedule", "mon 25:00"), ("group", "0")):
            with self.assertRaises(ValueError):
                module.edit_setting(settings, field, value)

    def test_chat_ids_are_normalized_and_group_changes_clear_topic(self):
        original = replace(Settings(), chat_id=-100456, message_thread_id=42, enabled=True)
        for value in ("100123", "-100123", " +100123 "):
            with self.subTest(value=value):
                settings = module.edit_setting(original, "group", value)
                self.assertEqual(settings.chat_id, -100123)
                self.assertIsNone(settings.message_thread_id)
                self.assertFalse(settings.enabled)
        for value in ("0", "-0", "abc", "1.5"):
            with self.assertRaises(ValueError):
                module.edit_setting(original, "group", value)

    async def test_topic_is_sent_to_group_but_not_private_preview(self):
        self.bot.settings = replace(Settings(), chat_id=-100123, message_thread_id=42, enabled=True)
        context = self.make_context()
        await self.bot.send_poll(context)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["message_thread_id"], 42)
        context.job = SimpleNamespace(data=1)
        await self.bot.scheduled_poll(context)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["message_thread_id"], 42)
        await self.bot.send_with_feedback(self.make_update(), context, preview=True)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["chat_id"], 123)
        self.assertNotIn("message_thread_id", context.bot.send_poll.call_args.kwargs)

    async def test_topic_destinations_in_same_group_stay_distinct_and_can_be_reused(self):
        self.bot.settings = replace(Settings(), chat_id=-100123, message_thread_id=42, destination_label="Studio")
        second = self.add_poll(name="Evening", chat_id=-100123, message_thread_id=84, destination_label="Studio")
        third = self.add_poll(name="Weekend")
        context = self.make_context()
        update = self.make_update(f"destination:{third}")
        await self.bot.button(update, context)
        rows = update.effective_message.reply_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([rows[0][0].text, rows[1][0].text], ["Studio > Topic 42", "Studio > Topic 84"])
        await self.bot.button(self.make_update(f"dest:{third}:{second}"), context)
        self.assertEqual(self.bot.store.load(third).message_thread_id, 84)

    async def test_chatid_reports_topic_id(self):
        update = self.make_update()
        update.effective_chat = SimpleNamespace(id=-100123, type="supergroup")
        update.effective_message.message_thread_id = 42
        await self.bot.chatid(update, self.make_context())
        update.effective_message.reply_text.assert_awaited_once_with("Chat ID: -100123\nTopic ID: 42")

    async def test_topic_can_be_configured_and_cleared_in_forum_group(self):
        self.bot.settings = replace(Settings(), chat_id=-100789, enabled=True)
        context = self.make_context()
        context.bot.get_chat.return_value = Chat(-100789, "supergroup", title="Studio", is_forum=True)
        await self.bot.button(self.make_update("topic:1"), context)
        await self.bot.text(self.make_update(text="42"), context)
        settings = self.bot.store.load()
        self.assertEqual(settings.message_thread_id, 42)
        self.assertFalse(settings.enabled)
        await self.bot.button(self.make_update("topic:1"), context)
        await self.bot.text(self.make_update(text="0"), context)
        self.assertIsNone(self.bot.store.load().message_thread_id)
        self.assertEqual(context.bot.get_chat.await_count, 1)

    async def test_topic_rejected_for_nonforum_group_without_saving(self):
        self.bot.settings = replace(Settings(), chat_id=-100789)
        self.bot.store.save(self.bot.settings)
        context = self.make_context()
        await self.bot.button(self.make_update("topic:1"), context)
        update = self.make_update(text="42")
        await self.bot.text(update, context)
        self.assertIsNone(self.bot.store.load().message_thread_id)
        self.assertIn("forum topics", update.effective_message.reply_text.call_args.args[0])

    def test_invalid_topic_inputs_are_rejected(self):
        settings = replace(Settings(), chat_id=-100123)
        for value in ("-1", "abc", "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.edit_setting(settings, "topic", value)
        for settings in (Settings(), replace(settings, is_channel=True)):
            with self.assertRaises(ValueError):
                module.edit_setting(settings, "topic", "42")

    async def test_positive_chat_input_is_normalized_before_lookup(self):
        context = self.make_context()
        await self.bot.button(self.make_update("group:1"), context)
        await self.bot.text(self.make_update(text="100789"), context)
        context.bot.get_chat.assert_awaited_once_with(-100789)
        self.assertEqual(self.bot.store.load().chat_id, -100789)

    def test_reschedule_replaces_job_and_uses_local_timezone(self):
        application = SimpleNamespace(job_queue=Mock())
        old_job = Mock()
        application.job_queue.get_jobs_by_name.return_value = (old_job,)
        self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123, timezone="Europe/Warsaw")
        self.bot.reschedule(application)
        old_job.schedule_removal.assert_called_once()
        arguments = application.job_queue.run_daily.call_args.kwargs
        self.assertEqual(arguments["days"], (1, 4))
        self.assertEqual(str(arguments["time"].tzinfo), "Europe/Warsaw")
        self.assertEqual(arguments["time"].hour, 13)
        self.bot.settings = replace(self.bot.settings, enabled=False)
        application.job_queue.run_daily.reset_mock()
        self.bot.reschedule(application)
        application.job_queue.run_daily.assert_not_called()

    def test_interval_schedule_can_switch_back_to_weekdays(self):
        settings = module.edit_setting(Settings(), "schedule", "every 2 minutes")
        self.assertEqual(settings.interval_minutes, 2)
        settings = module.edit_setting(settings, "schedule", "mon,thu 13:00")
        self.assertIsNone(settings.interval_minutes)
        for value in ("every 0 minutes", "every bad minutes", "every 1.5 minutes"):
            with self.assertRaises(ValueError):
                module.edit_setting(settings, "schedule", value)

    def test_weekday_schedule_accepts_both_formats(self):
        for value in ("mon tue wed, 14-31", "mon,tue,wed 14:31", " MON TUE WED,   14-31 "):
            with self.subTest(value=value):
                settings = module.edit_setting(Settings(), "schedule", value)
                self.assertEqual(settings.days, (0, 1, 2))
                self.assertEqual((settings.hour, settings.minute), (14, 31))

    def test_switching_schedule_formats_clears_previous_mode(self):
        settings = module.edit_setting(Settings(), "schedule", "every 2 minutes")
        settings = module.edit_setting(settings, "schedule", "cron: 0 12 * * 1,4")
        self.assertEqual(settings.cron_expression, "0 12 * * 1,4")
        self.assertIsNone(settings.interval_minutes)
        settings = module.edit_setting(settings, "schedule", "mon tue wed, 14-31")
        self.assertIsNone(settings.cron_expression)
        settings = module.edit_setting(settings, "schedule", "CRON: */2 * * * *")
        settings = module.edit_setting(settings, "schedule", "every 2 minutes")
        self.assertIsNone(settings.cron_expression)
        self.assertEqual(settings.interval_minutes, 2)

    def test_real_cron_scheduler_uses_standard_weekdays_and_timezone(self):
        application = module.Application.builder().token("123:offline-test-token").build()
        self.bot.settings = replace(
            Settings(), enabled=True, chat_id=-100123, timezone="Etc/GMT-4", cron_expression="0 12 * * 1,4",
        )
        self.bot.reschedule(application)
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll:1")
        self.assertEqual(len(jobs), 1)
        trigger = jobs[0].job.trigger
        now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
        first = trigger.get_next_fire_time(None, now)
        self.assertEqual(first.isoformat(), "2026-11-05T12:00:00+04:00")
        second = trigger.get_next_fire_time(first, first)
        self.assertEqual(second.isoformat(), "2026-11-09T12:00:00+04:00")
        self.assertIn("cron: 0 12 * * 1,4", self.bot.panel()[0])

    def test_invalid_schedule_does_not_change_persisted_settings(self):
        for value in ("mon tue, 25-00", "bad, 14-31", "cron:", "cron: 0 25 * * *", "cron: 0 12 * * 9", "cron: 0 0 12 * * *"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings = module.edit_setting(self.bot.settings, "schedule", value)
                self.bot.store.save(settings)
        self.assertEqual(self.bot.store.load(), Settings())

    def test_real_interval_scheduler_uses_two_minutes(self):
        application = module.Application.builder().token("123:offline-test-token").build()
        self.bot.settings = replace(
            Settings(), enabled=True, chat_id=-100123, timezone="Etc/GMT-4", interval_minutes=2,
        )
        self.bot.reschedule(application)
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll:1")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job.trigger.interval.total_seconds(), 120)
        text, _ = self.bot.panel()
        self.assertIn("Every 2 minutes", text)

    async def test_preview_and_send_use_voting_settings(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_poll=AsyncMock()))
        self.bot.settings = replace(Settings(), chat_id=-100123, anonymous=False, multiple=True)
        await self.bot.send_poll(context, chat_id=123)
        arguments = context.bot.send_poll.call_args.kwargs
        self.assertEqual(arguments["chat_id"], 123)
        self.assertFalse(arguments["is_anonymous"])
        self.assertTrue(arguments["allows_multiple_answers"])
        await self.bot.send_poll(context)
        self.assertEqual(context.bot.send_poll.call_args.kwargs["chat_id"], -100123)

    async def test_disabled_schedule_does_not_send(self):
        context = SimpleNamespace(bot=SimpleNamespace(send_poll=AsyncMock()))
        await self.bot.scheduled_poll(context)
        context.bot.send_poll.assert_not_called()

    async def test_forwarded_channel_selects_destination_after_permission_check(self):
        self.bot.settings = replace(Settings(), chat_id=-100123, message_thread_id=42)
        chat = module.MessageOriginChannel(
            date=datetime.now(timezone.utc),
            chat=Chat(-100456, "channel", title="Private Test"),
            message_id=1,
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(type="private"),
            effective_message=SimpleNamespace(forward_origin=chat, reply_text=AsyncMock()),
        )
        queue = Mock()
        queue.get_jobs_by_name.return_value = ()
        context = SimpleNamespace(
            user_data={}, application=SimpleNamespace(job_queue=queue),
            bot=SimpleNamespace(id=789, get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator", can_post_messages=True))),
        )
        await self.bot.forwarded_channel(update, context)
        self.assertEqual(self.bot.settings.chat_id, -100456)
        self.assertIsNone(self.bot.settings.message_thread_id)
        self.assertTrue(self.bot.settings.anonymous)
        self.assertFalse(self.bot.settings.enabled)
        self.assertEqual(self.bot.store.load(), self.bot.settings)
        update.effective_user.id = 456
        context.bot.get_chat_member.reset_mock()
        await self.bot.forwarded_channel(update, context)
        context.bot.get_chat_member.assert_not_called()

    async def test_forwarded_channel_rejects_missing_post_permission(self):
        origin = module.MessageOriginChannel(datetime.now(timezone.utc), Chat(-100456, "channel"), 1)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(type="private"),
            effective_message=SimpleNamespace(forward_origin=origin, reply_text=AsyncMock()),
        )
        context = SimpleNamespace(user_data={}, bot=SimpleNamespace(id=789, get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))))
        await self.bot.forwarded_channel(update, context)
        self.assertIsNone(self.bot.settings.chat_id)

    def test_real_scheduler_accepts_settings(self):
        application = module.Application.builder().token("123:offline-test-token").build()
        self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123, timezone="Europe/Warsaw")
        self.bot.reschedule(application)
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll:1")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(str(jobs[0].job.trigger.timezone), "Europe/Warsaw")


if __name__ == "__main__":
    unittest.main()