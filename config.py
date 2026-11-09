import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.base import BaseTrigger
from croniter import CroniterBadDateError, croniter


DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class CrontabTrigger(BaseTrigger):
    def __init__(self, expression: str, timezone: str):
        if len(expression) > 256 or len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise ValueError("Use five cron fields: minute hour day-of-month month day-of-week.")
        self.expression = expression
        self.timezone = ZoneInfo(timezone)

    def get_next_fire_time(self, previous_fire_time: datetime | None, now: datetime) -> datetime | None:
        start = previous_fire_time if previous_fire_time is not None else now - timedelta(microseconds=1)
        try:
            return croniter(
                self.expression, start.astimezone(self.timezone), max_years_between_matches=50,
            ).get_next(datetime)
        except CroniterBadDateError:
            return None


@dataclass(frozen=True)
class Settings:
    question: str = "Are you joining on {date}?"
    options: tuple[str, ...] = ("Yes", "No")
    days: tuple[int, ...] = (0, 3)
    hour: int = 13
    minute: int = 0
    timezone: str = "UTC"
    chat_id: int | None = None
    anonymous: bool = True
    multiple: bool = False
    enabled: bool = False
    interval_minutes: int | None = None
    cron_expression: str | None = None
    name: str = "Poll 1"
    destination_label: str = ""
    is_channel: bool = False
    message_thread_id: int | None = None

    def validate(self) -> None:
        if self.message_thread_id is not None:
            if type(self.message_thread_id) is not int or self.message_thread_id <= 0:
                raise ValueError("Topic ID must be a positive whole number.")
            if self.chat_id is None or self.is_channel:
                raise ValueError("Topics require a group destination, not a channel.")
        if not self.name.strip() or len(self.name) > 60 or "\n" in self.name:
            raise ValueError("Poll name must contain 1 to 60 characters on one line.")
        if len(self.destination_label) > 60 or "\n" in self.destination_label:
            raise ValueError("Destination label must contain at most 60 characters on one line.")
        if self.is_channel and not self.anonymous:
            raise ValueError("Channel polls must be anonymous.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Use an IANA time zone, such as Europe/Warsaw.") from error
        time(self.hour, self.minute)
        if self.interval_minutes is not None and (
            type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 10080
        ):
            raise ValueError("Interval must be 1 to 10080 whole minutes.")
        if self.cron_expression is not None:
            if self.interval_minutes is not None:
                raise ValueError("Select either a cron schedule or an interval, not both.")
            trigger = CrontabTrigger(self.cron_expression, self.timezone)
            if trigger.get_next_fire_time(None, datetime.now(ZoneInfo(self.timezone))) is None:
                raise ValueError("Cron schedule has no occurrence within the next 50 years.")
        if not self.days or any(day not in range(7) for day in self.days):
            raise ValueError("Select at least one valid weekday.")
        if not 2 <= len(self.options) <= 10:
            raise ValueError("Use 2 to 10 answer options.")
        if any(not option.strip() or len(option) > 100 for option in self.options):
            raise ValueError("Each answer must contain 1 to 100 characters.")
        for _, field, spec, conversion in Formatter().parse(self.question_template):
            if field is not None and (field not in {"date", "weekday", "day_of_week", "time"} or spec or conversion):
                raise ValueError("Supported variables: {date}, {weekday}, {{day_of_week}}, {time}.")
        self.render()
        if self.enabled and self.chat_id is None:
            raise ValueError("Set the destination group before enabling the schedule.")

    @property
    def destination_name(self) -> str:
        label = self.destination_label or ("Unnamed destination" if self.chat_id else "Not set")
        return f"{label} > Topic {self.message_thread_id}" if self.message_thread_id is not None else label

    @property
    def question_template(self) -> str:
        return self.question.replace("{{day_of_week}}", "{day_of_week}")

    def render(self, now: datetime | None = None) -> str:
        local = (now or datetime.now(ZoneInfo(self.timezone))).astimezone(ZoneInfo(self.timezone))
        question = self.question_template.format(
            date=local.strftime("%Y-%m-%d"),
            weekday=local.strftime("%A"),
            day_of_week=local.strftime("%A"),
            time=local.strftime("%H:%M"),
        )
        if not question.strip() or len(question) > 300:
            raise ValueError("Rendered question must contain 1 to 300 characters.")
        return question

    @property
    def scheduler_days(self) -> tuple[int, ...]:
        return tuple(sorted((day + 1) % 7 for day in self.days))


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            initialized = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'polls'").fetchone()
            connection.execute("CREATE TABLE IF NOT EXISTS polls (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)")
            if initialized is None:
                legacy = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'").fetchone()
                row = connection.execute("SELECT data FROM settings WHERE id = 1").fetchone() if legacy else None
                settings = self.decode(row[0]) if row else Settings()
                connection.execute("INSERT INTO polls (id, data) VALUES (1, ?)", (json.dumps(asdict(settings)),))

    @staticmethod
    def decode(data: str) -> Settings:
        values = json.loads(data)
        values["options"] = tuple(values["options"])
        values["days"] = tuple(values["days"])
        settings = Settings(**values)
        settings.validate()
        return settings

    def load(self, poll_id: int = 1) -> Settings:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            row = connection.execute("SELECT data FROM polls WHERE id = ?", (poll_id,)).fetchone()
        if row is None:
            raise ValueError("This poll no longer exists.")
        return self.decode(row[0])

    def list_polls(self) -> dict[int, Settings]:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            rows = connection.execute("SELECT id, data FROM polls ORDER BY id").fetchall()
        return {poll_id: self.decode(data) for poll_id, data in rows}

    def save(self, settings: Settings, poll_id: int = 1) -> None:
        settings.validate()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            result = connection.execute("UPDATE polls SET data = ? WHERE id = ?", (json.dumps(asdict(settings)), poll_id))
            if result.rowcount == 0:
                raise ValueError("This poll no longer exists.")

    def create(self, settings: Settings) -> int:
        settings.validate()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            result = connection.execute("INSERT INTO polls (data) VALUES (?)", (json.dumps(asdict(settings)),))
            assert result.lastrowid is not None
            return int(result.lastrowid)

    def delete(self, poll_id: int) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM polls WHERE id = ?", (poll_id,))