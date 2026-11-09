import tempfile
import unittest
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, replace
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

    def test_new_polls_default_to_nonanonymous_without_changing_saved_choices(self):
        self.assertFalse(Settings().anonymous)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            store = SettingsStore(path)
            self.assertFalse(store.load().anonymous)
            for anonymous in (True, False):
                settings = replace(Settings(), anonymous=anonymous)
                store.save(settings)
                self.assertEqual(SettingsStore(path).load().anonymous, anonymous)

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
            {"name": " "}, {"name": "a" * 61}, {"name": "One\nTwo"},
            {"destination_label": "a" * 61}, {"is_channel": True, "anonymous": False},
            {"message_thread_id": 0}, {"message_thread_id": -1},
            {"message_thread_id": True}, {"message_thread_id": 1.5},
            {"message_thread_id": 42},
            {"message_thread_id": 42, "chat_id": -100123, "is_channel": True},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(Settings(), **values).validate()

    def test_legacy_single_poll_migrates_once_without_losing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            legacy = replace(Settings(), chat_id=-100123, enabled=True, anonymous=True, cron_expression="0 12 * * 1,4")
            values = asdict(legacy)
            for field in ("name", "destination_label", "is_channel", "message_thread_id"):
                values.pop(field)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
                connection.execute("INSERT INTO settings VALUES (1, ?)", (json.dumps(values),))
            store = SettingsStore(path)
            self.assertEqual(store.list_polls(), {1: legacy})
            changed = replace(legacy, name="Morning class", destination_label="Main group")
            store.save(changed)
            self.assertEqual(SettingsStore(path).load(), changed)

    def test_topic_settings_persist_and_old_records_default_to_no_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            settings = replace(Settings(), chat_id=-100123, message_thread_id=42, destination_label="Studio")
            SettingsStore(path).save(settings)
            self.assertEqual(SettingsStore(path).load(), settings)
            values = asdict(settings)
            values.pop("message_thread_id")
            self.assertIsNone(SettingsStore.decode(json.dumps(values)).message_thread_id)

    def test_multiple_polls_are_independent_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.db"
            store = SettingsStore(path)
            second = replace(Settings(), name="Evening class", destination_label="Evening group", chat_id=-100456, hour=18)
            poll_id = store.create(second)
            store.save(replace(Settings(), name="Morning class", hour=9))
            reloaded = SettingsStore(path)
            self.assertEqual(len(reloaded.list_polls()), 2)
            self.assertEqual(reloaded.load(poll_id), second)
            self.assertEqual(reloaded.load().hour, 9)
            reloaded.delete(poll_id)
            with self.assertRaises(ValueError):
                reloaded.load(poll_id)
            with self.assertRaises(ValueError):
                reloaded.save(second, poll_id)
            reloaded.delete(1)
            self.assertEqual(SettingsStore(path).list_polls(), {})

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