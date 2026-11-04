import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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

    def test_real_scheduler_accepts_settings(self):
        application = module.Application.builder().token("123:offline-test-token").build()
        self.bot.settings = replace(Settings(), enabled=True, chat_id=-100123, timezone="Europe/Warsaw")
        self.bot.reschedule(application)
        jobs = application.job_queue.get_jobs_by_name("scheduled_poll")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(str(jobs[0].job.trigger.timezone), "Europe/Warsaw")


if __name__ == "__main__":
    unittest.main()