import importlib.util
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telegram import Chat

from config import Settings, SettingsStore


spec = importlib.util.spec_from_file_location("vote_bot", Path(__file__).resolve().parents[1] / "vote-bot.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bot = module.PollBot(SettingsStore(Path(self.directory.name) / "settings.db"), {123})

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
        for field, value in (("schedule", "bad"), ("schedule", "mon 25:00"), ("group", "123")):
            with self.assertRaises(ValueError):
                module.edit_setting(settings, field, value)

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
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll")
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
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll")
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
        context = SimpleNamespace(bot=SimpleNamespace(id=789, get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member"))))
        await self.bot.forwarded_channel(update, context)
        self.assertIsNone(self.bot.settings.chat_id)

    def test_real_scheduler_accepts_settings(self):
        application = module.Application.builder().token("123:offline-test-token").build()
        self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123, timezone="Europe/Warsaw")
        self.bot.reschedule(application)
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(str(jobs[0].job.trigger.timezone), "Europe/Warsaw")


if __name__ == "__main__":
    unittest.main()