import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config import Settings, SettingsStore


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
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Settings(), **values).validate()

    def test_invalid_update_does_not_replace_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.db")
            store.save(Settings())
            with self.assertRaises(ValueError):
                store.save(replace(Settings(), question="{bad}"))
            self.assertEqual(store.load(), Settings())


if __name__ == "__main__":
    unittest.main()