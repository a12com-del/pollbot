import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config import CrontabTrigger, Settings, SettingsStore


class SettingsTests(unittest.TestCase):
    def test_variables_use_configured_local_time(self):
        settings = Settings(question="{date} {weekday} {time}", timezone="Europe/Warsaw")
        now = datetime(2026, 11, 4, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(settings.render(now), "2026-11-05 Thursday 00:30")

    def test_monday_thursday_scheduler_mapping(self):
        self.assertEqual(Settings().scheduler_days, (1, 4))
        self.assertEqual(replace(Settings(), days=(6,)).scheduler_days, (0,))

    def test_requested_question_uses_gmt_plus_four_weekday(self):
        settings = Settings(
            question="Will you visit class  {{day_of_week}}?",
            timezone="Etc/GMT-4", interval_minutes=2,
        )
        settings.validate()
        now = datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc)
        self.assertEqual(settings.render(now), "Will you visit class  Thursday?")

    def test_settings_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            settings = replace(Settings(), chat_id=-100123456, enabled=True, multiple=True, interval_minutes=2)
            SettingsStore(path).save(settings)
            self.assertEqual(SettingsStore(path).load(), settings)

    def test_reject_invalid_settings(self):
        invalid = (
            {"timezone": "invalid"}, {"days": ()}, {"hour": 24},
            {"options": ("Yes",)}, {"options": ("Yes", " ")},
            {"question": "{unknown}"}, {"question": "{date.__class__}"},
            {"question": "{date!r}"}, {"question": "{"},
            {"question": " "}, {"question": "a" * 301}, {"enabled": True},
            {"interval_minutes": 0}, {"interval_minutes": -1},
            {"interval_minutes": 1.5}, {"interval_minutes": True},
            {"interval_minutes": 10081},
            {"cron_expression": "0 12 * * 1,4", "interval_minutes": 2},
            {"cron_expression": "0 12 31 2 *"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Settings(), **values).validate()

    def test_cron_settings_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.db")
            settings = replace(Settings(), cron_expression="0 12 * * 1,4")
            store.save(settings)
            self.assertEqual(store.load(), settings)

    def test_standard_cron_sunday_aliases(self):
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        for sunday in (0, 7):
            trigger = CrontabTrigger(f"0 12 * * {sunday}", "UTC")
            self.assertEqual(trigger.get_next_fire_time(None, now).isoformat(), "2026-11-08T12:00:00+00:00")

    def test_cron_day_of_month_and_week_use_standard_or_semantics(self):
        trigger = CrontabTrigger("0 12 1 * 1", "UTC")
        now = datetime(2026, 11, 4, tzinfo=timezone.utc)
        self.assertEqual(trigger.get_next_fire_time(None, now).isoformat(), "2026-11-09T12:00:00+00:00")

    def test_cron_steps_ranges_and_exact_boundary(self):
        trigger = CrontabTrigger("*/15 9-10 * * mon-fri", "UTC")
        now = datetime(2026, 11, 4, 9, 7, tzinfo=timezone.utc)
        first = trigger.get_next_fire_time(None, now)
        self.assertEqual(first.isoformat(), "2026-11-04T09:15:00+00:00")
        self.assertEqual(trigger.get_next_fire_time(None, first), first)
        self.assertEqual(trigger.get_next_fire_time(first, first).isoformat(), "2026-11-04T09:30:00+00:00")

    def test_invalid_update_does_not_replace_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.db")
            store.save(Settings())
            with self.assertRaises(ValueError):
                store.save(replace(Settings(), question="{bad}"))
            self.assertEqual(store.load(), Settings())


if __name__ == "__main__":
    unittest.main()