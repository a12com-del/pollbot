# Scheduled Telegram Poll Bot

A Python bot with a private Telegram admin panel and persistent settings.
One configured poll is posted to one destination using a weekday/time or cron
schedule. Existing minute-based interval schedules are also supported.

## Container Setup (Podman or Docker)

The image includes Python and all bot dependencies, runs as UID/GID `10001`,
and needs no published ports. The build context excludes `.env`, SQLite data,
the local virtual environment, Git files, and tests. Keep `requirements.txt`
consistent with the inline dependencies in `vote-bot.py` when updating them.

On macOS, start your existing Podman machine with `podman machine start` if
it is stopped. Then build and run from this workspace:

```sh
podman build --tag vote-bot:local .
podman volume create vote-bot-data
podman run --detach --name vote-bot \
   --restart=unless-stopped \
   --env-file .env \
   --read-only \
   --cap-drop=ALL \
   --security-opt=no-new-privileges \
   --tmpfs /tmp:rw,nosuid,noexec,size=64m \
   --volume vote-bot-data:/app/data \
   vote-bot:local
```

The token is passed at runtime, never copied into the image. Users with access
to the container engine can inspect runtime environment variables; restrict
engine access and protect `.env`. Recreate the container after changing `.env`.

Settings persist in the named volume even after removing the container. A fresh
volume starts with defaults and posting disabled. Do not run the host Python
bot or another container with the same token at the same time.
After migration, the container volume is the active settings store. Later admin
changes update that volume, not the original host `data/settings.db`.

### Import Existing Settings

Before the first container start, stop the host bot and import its local SQLite
database into an empty named volume. Do not overwrite a volume already in use:

```sh
podman run --rm --user 0 \
   --volume "$PWD/data:/import:ro" \
   --volume vote-bot-data:/app/data \
   --entrypoint python vote-bot:local -c '
import os
import sqlite3
from contextlib import closing
with closing(sqlite3.connect("file:/import/settings.db?mode=ro", uri=True)) as source:
      with closing(sqlite3.connect("/app/data/settings.db")) as target:
            source.backup(target)
os.chown("/app/data/settings.db", 10001, 10001)
os.chmod("/app/data/settings.db", 0o600)
'
```

Importing preserves the destination, question, schedule, voting settings, and
enabled state. The original local database is left unchanged.

### Container Operations

```sh
podman logs --tail 50 vote-bot
podman stop --time 30 vote-bot
podman start vote-bot
```

To update, rebuild the image, stop and remove the old container, then repeat
the run command using the same named volume. Stop before replacing it to avoid
two simultaneous Telegram pollers. Back up the SQLite database using SQLite's
backup API; don't copy a live database file directly.

### Permission Denied on Startup

If Python cannot open `/app/vote-bot.py`, a restrictive source file mode may
have been inherited by an older image. The Dockerfile now explicitly copies
application files with mode `0644` so runtime UID `10001` can read them.
Do not work around this by running the bot as root or using `chmod 777`.

After updating the Dockerfile on the VM, rebuild and replace the container:

```sh
podman build --no-cache --tag vote-bot:local .
podman stop --time 30 vote-bot
podman rm vote-bot
```

Repeat the original `podman run` command with the same `vote-bot-data` volume.
Do not delete or re-import the volume. Rebuilding alone does not update an
existing container. If you additionally bind-mount source code onto `/app`, its
host permissions and, on SELinux hosts, volume labels must also allow access;
image permissions cannot override a bind mount.

On a Linux VM with Docker, the same commands work by replacing `podman` with
`docker`. Build on the target VM to match its CPU architecture; a locally built
Apple Silicon image is ARM64. With Podman on Linux, use a systemd/Quadlet service
for startup after reboot. A macOS Podman machine must remain running, and sleep
still interrupts delivery.

### Container Tests

```sh
podman run --rm --network none --read-only \
   --cap-drop=ALL --security-opt=no-new-privileges \
   --tmpfs /tmp:rw,nosuid,noexec,size=64m \
   --volume "$PWD/tests:/app/tests:ro" \
   vote-bot:local python -W error::ResourceWarning -m unittest discover -s /app/tests -v
```

## Setup

Use stable Python 3.11 or newer. This workspace already has dependencies in
`.venv`; its current interpreter is stable Python 3.14. For a fresh installation,
`uv run vote-bot.py` installs the dependencies declared in the script automatically.
Alternatively, install `python-telegram-bot[job-queue]>=22,<23` and
`python-dotenv>=1,<2`, plus `croniter>=6,<7`, into a virtual environment.

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
   ID using **Destination** in the private panel. For a channel, make the bot
   an administrator with permission to post messages, then forward an original
   channel post to the bot privately. This selects the channel, verifies posting
   permission, and disables posting until you explicitly enable it. Channel polls
   must be anonymous. Forwarding must be permitted by the channel.
7. Set the question, answers, schedule, time zone, and voting preferences.
   Use **Preview** to receive a real test poll privately, then **Enable**.

Keep the token in your local `.env` or process environment, not in source code,
chat messages, or Git. The token and admin IDs are never requested in the panel.
Rotate any exposed token through **@BotFather** and update the local `.env`.
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
Use **Schedule** in the private admin panel to enter either format:

| Format | Example | Meaning |
| --- | --- | --- |
| Weekdays and time | `mon tue wed, 14-31` | Monday, Tuesday, Wednesday at 14:31 |
| Five-field cron | `cron: 0 12 * * 1,4` | Monday and Thursday at 12:00 |

Weekday names are `mon,tue,wed,thu,fri,sat,sun`. Separate days with spaces or
commas, and use `HH-MM` or `HH:MM` for the time. The older `mon,thu 13:00`
format still works.

Cron fields are **minute hour day-of-month month day-of-week**. Standard cron
weekday numbering applies: Sunday is `0` or `7`, Monday is `1`, Thursday is `4`.
Wildcards, lists, ranges, and steps work, for example `cron: */15 9-17 * * 1-5`.
When both day-of-month and day-of-week are restricted, either may match
(standard cron OR semantics). Seconds and year fields are not supported.
Both formats use the configured time zone. Invalid schedules are rejected
without changing the saved configuration. Switching formats replaces the previous
schedule; it does not create an additional job.

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

## Private Testing

Keep real bot usernames, admin IDs, destination names, and test-specific settings
in the Git-ignored `PRIVATE_TESTING.md`, when present. Keep tokens only in `.env`
or the runtime environment, not in testing notes. Private notes are also excluded
from the container build context.

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
admin authorization, preview/group destinations, voting settings, both schedule
formats, standard cron semantics, and scheduler integration. They do not require
a token and do not send real Telegram messages.