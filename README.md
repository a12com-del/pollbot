# Scheduled Telegram Poll Bot

A Python bot with a private Telegram admin panel and persistent settings.
One configured poll is posted to one group on selected weekdays at a local time.
Minute-based interval schedules are also supported.

## Setup

Use stable Python 3.11 or newer. This workspace already has dependencies in
`.venv`; its current interpreter is Python 3.15 alpha. For a fresh installation,
`uv run vote-bot.py` installs the dependencies declared in the script automatically.
Alternatively, install `python-telegram-bot[job-queue]>=22,<23` and
`python-dotenv>=1,<2` into a virtual environment.

1. Create a bot with Telegram's official **@BotFather**, using `/newbot`.
2. Edit the local `.env`: set `TELEGRAM_BOT_TOKEN` to the token and
   `ADMIN_USER_IDS` to your numeric Telegram user ID. Multiple admins can be
   separated by commas. You can find your ID through a Telegram data export
   in Telegram Desktop (Settings > Advanced > Export Telegram data).
   `.env.example` documents the required environment variables for new checkouts.
3. Add the bot to your group and allow it to send messages and polls.
4. Run from this workspace:

   ```sh
   .venv/bin/python vote-bot.py
   ```

5. Open a private conversation with the bot and send `/start` or `/admin`.
6. In the target group, send `/chatid@YourBotUsername`. Enter that negative
   ID using **Destination group** in the private panel.
7. Set the question, answers, schedule, time zone, and voting preferences.
   Use **Preview** to receive a real test poll privately, then **Enable**.

Keep the token in your local `.env` or process environment, not in source code,
chat messages, or Git. The token and admin IDs are never requested in the panel.
Only allowlisted users may administer the bot. Admin settings work only in
private chats; `/chatid` also works in groups for those admins.

## Configuration

The panel has buttons for question, answer options, schedule, time zone,
destination, anonymous voting, multiple answers, enabling/disabling posting,
preview, and immediate posting with confirmation. `/cancel` abandons an edit.
Changes persist in `data/settings.db` and apply without restarting.

The default schedule is **Monday and Thursday at 13:00**, with posting disabled.
The default time zone is **UTC**; select your actual IANA time zone, such as
`Europe/Warsaw`, before enabling. Daylight-saving changes follow that zone.
Enter a schedule as `mon,thu 13:00`. Supported days are
`mon,tue,wed,thu,fri,sat,sun`.
For interval posting, enter `every 2 minutes` in **Schedule**. The first poll
is sent two minutes after enabling or restarting; subsequent polls follow that
interval, regardless of weekdays. Changing back to `mon,thu 13:00` restores
weekday scheduling.

Answer options are entered one per line (2 to 10 options, each at most 100
characters). Anonymous voting defaults to on, multiple answers to off.
Polls stay open until stopped in Telegram; an automatic closing time is not
implemented.

Question variables are evaluated at posting time in the configured time zone:

| Variable | Example |
| --- | --- |
| `{date}` | `2026-11-05` |
| `{weekday}` | `Thursday` |
| `{{day_of_week}}` or `{day_of_week}` | `Thursday` |
| `{time}` | `13:00` |

For example: `Are you joining on {weekday}, {date}?`
Use `{{` and `}}` for literal braces. The rendered question must be at most
300 characters. Weekday names use the process locale (normally English).
The exact `{{day_of_week}}` placeholder is an exception to the literal-brace rule.

## Private Test Settings

For `@YourBotUsername`, use admin ID `YOUR_ADMIN_USER_ID`, schedule `every 2 minutes`,
time zone `Etc/GMT-4` (fixed GMT+4; the IANA sign is reversed), and question:

```text
Will you visit class  {{day_of_week}}?
```

Rotate any token shared in chat through **@BotFather** and enter its replacement
directly in the ignored `.env`. Add the bot to **Private Test**, start a private
chat with it, and send `/chatid@YourBotUsername` in the group. A group name is
not a chat ID. Set the returned negative ID as **Destination group** and use
**Preview** before **Enable**. Disable posting after the test to stop new polls.

## Running Reliably

The process must remain running with network access. Sleeping or shutting down
your Mac stops delivery. Restarting reloads settings and schedules the next
future occurrence; it does not replay missed polls. A running scheduler allows
up to 60 seconds of lateness. Use an always-on host for reliable production
delivery, and run only one instance with this token and database.

Scheduled failures are logged without the token and an attempt is made to notify
each admin privately. Admins must have started a chat with the bot to receive
notifications. Failed sends are not retried automatically: a network timeout
can occur after Telegram accepted the poll, so inspect the group before retrying.
For schedules near daylight-saving transitions, use an unambiguous daytime
hour like the default 13:00.

## Tests

```sh
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
```

Tests cover persistence, validation, local date rendering, weekday mapping,
admin authorization, preview/group destinations, voting settings, and scheduler
integration. They do not require a token and do not send real Telegram messages.