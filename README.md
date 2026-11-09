# Scheduled Telegram Poll Bot

A Python bot with a private Telegram admin panel and persistent settings.
Manage multiple named polls, each with its own destination, question, answers,
time zone, and weekday/time or cron schedule. Existing minute-based interval
schedules are also supported. Destinations are shown by editable labels.

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

### Linux VM Service (Podman Quadlet)

Run these steps on the Linux VM, not on macOS. Use a Podman installation with
Quadlet support and cgroup v2. Check `podman --version` and
`podman info --format '{{.Host.CgroupsVersion}}'` (the latter must report `v2`).
The image and data volume must already exist.

Use **the same Linux account and rootless/rootful mode** as your current
container. Rootless Podman and `sudo podman` have separate images and volumes;
switching modes can silently create an empty settings volume. Do not run both
services or a second manual container with the same bot token.

#### Rootless Service (Podman Without sudo)

1. Log in as the account that owns the existing container. From the repository,
   check its image, volume, and absolute environment-file path:

   ```sh
   podman image exists localhost/vote-bot:local
   podman volume exists vote-bot-data
   realpath .env
   chmod 600 .env
   mkdir -p ~/.config/containers/systemd
   ```

2. Create `~/.config/containers/systemd/vote-bot.container` with the following
   content. Replace `/absolute/path/to/pollbot/.env` with the path above; it must
   be readable by this Linux account. Keep tokens out of the unit file.

   ```ini
   [Unit]
   Description=Scheduled Telegram poll bot
   StartLimitIntervalSec=0

   [Container]
   Image=localhost/vote-bot:local
   Pull=never
   ContainerName=vote-bot
   EnvironmentFile=/absolute/path/to/pollbot/.env
   Volume=vote-bot-data:/app/data
   User=10001
   Group=10001
   ReadOnly=true
   DropCapability=all
   NoNewPrivileges=true
   Tmpfs=/tmp:rw,nosuid,noexec,size=64m
   LogDriver=journald
   StopTimeout=30

   [Service]
   Restart=always
   RestartSec=10
   TimeoutStartSec=120
   TimeoutStopSec=60

   [Install]
   WantedBy=default.target
   ```

3. Enable lingering so this account's systemd user manager starts at boot and
   stays running after logout. Run the administrative command yourself:

   ```sh
   sudo loginctl enable-linger "$(id -un)"
   loginctl show-user "$(id -un)" -p Linger
   ```

   Confirm `Linger=yes`. Stop and remove the old manually created container
   before starting the service, without deleting its volume:

   ```sh
   podman stop --time 30 vote-bot
   podman rm vote-bot
   systemctl --user daemon-reload
   systemctl --user start vote-bot.service
   systemctl --user status vote-bot.service --no-pager
   ```

   Skip the stop/remove commands only if the old container is already absent.
   The `[Install]` section configures boot startup during generation; do **not**
   run `systemctl --user enable vote-bot.service` on this generated service.

4. Use systemd, rather than `podman start/stop`, from now on:

   ```sh
   journalctl --user -u vote-bot.service -n 50 --no-pager
   systemctl --user restart vote-bot.service
   systemctl --user stop vote-bot.service
   ```

   After an image rebuild or `.env` change, restart the service. After editing
   the Quadlet file, run `systemctl --user daemon-reload` and then restart it.
   `Restart=always` recovers process exits, but a deliberate `systemctl stop`
   stays stopped until started again or the next boot. To disable future boot
   startup, stop the service, remove its `[Install]` section, and daemon-reload.

#### Rootful Service (Existing Container Uses sudo podman)

Do not switch to rootless for an existing rootful deployment. As an administrator,
check `sudo podman image exists localhost/vote-bot:local` and
`sudo podman volume exists vote-bot-data`. Install the same Quadlet content in
`/etc/containers/systemd/vote-bot.container`, using an absolute `.env` path
readable by root and changing `WantedBy=default.target` to
`WantedBy=multi-user.target`. The container still runs as UID/GID `10001`.
Rootful system services do not need user lingering.

Run on the VM:

```sh
sudo podman stop --time 30 vote-bot
sudo podman rm vote-bot
sudo systemctl daemon-reload
sudo systemctl start vote-bot.service
sudo systemctl status vote-bot.service --no-pager
sudo journalctl -u vote-bot.service -n 50 --no-pager
```

Do not run `systemctl enable` for the generated service. For later management,
use `sudo systemctl restart vote-bot.service` or
`sudo systemctl stop vote-bot.service`, and rebuild with `sudo podman` so the
updated image remains in the same rootful storage.

#### Verify Reboot Recovery

When a brief interruption is acceptable, run `sudo reboot` on the VM.
After reconnecting, verify a rootless deployment with:

```sh
loginctl show-user "$(id -un)" -p Linger
systemctl --user is-active vote-bot.service
journalctl --user -b -u vote-bot.service -n 50 --no-pager
podman ps --filter name=vote-bot
```

For rootful deployments, use `sudo systemctl is-active vote-bot.service`,
`sudo journalctl -b -u vote-bot.service -n 50 --no-pager`, and
`sudo podman ps --filter name=vote-bot`. Confirm `active`, inspect this boot's
startup logs, and check that the private `/admin` panel responds with preserved
settings. For rootless deployments, lingering must remain enabled; do not rely
on an SSH login to start the service.

This starts the bot after reboot; it does not replay polls missed during downtime.
If `vote-bot.service` is not found after daemon-reload, check the Quadlet location,
installed Podman version, and generator diagnostics. The generator is commonly
`/usr/lib/systemd/system-generators/podman-system-generator`; use its
`--user --dryrun` options for rootless diagnostics, or `sudo` and `--dryrun`
without `--user` for rootful diagnostics. Locations can differ by distribution.
See the [official Quadlet documentation](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)
for supported options and troubleshooting.

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
6. Select a poll in `/admin`, or choose **New poll** and name it. In the target
   group, send `/chatid@YourBotUsername`. Enter its full chat ID using
   **Destination** in the private panel; a missing minus sign is added
   automatically. For a channel, make the bot
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

`/admin` opens the poll list. Select a poll to manage its question, answer options,
schedule, time zone, destination, anonymous voting, multiple answers, posting
state, previews, and immediate sending. Each poll has its own settings and
scheduled job. `/cancel` abandons an edit. Changes persist in `data/settings.db`
and apply without restarting.

### Multiple Polls and Destination Labels

- Use **New poll** to add a disabled poll, then give it a name. **Name** renames
   an existing poll. **All polls** returns to the list; long lists have pagination.
- Use **Destination** to choose a labelled destination already used by another
   poll, or **Enter chat ID** to register one. The bot looks up the Telegram group
   or channel title automatically. **Edit label** sets a friendly label for that
   poll's destination, such as `Main studio` or `Weekend class`.
- You can also forward an original channel post while a poll is selected.
   With several polls, select the intended poll first to avoid changing the wrong
   destination. Channel polls remain anonymous.
- Changing a destination disables that poll until you explicitly enable it.
   A label change alone does not disable posting. Destination menus and poll
   panels show labels instead of numeric chat IDs.
- Configure **Schedule**, **Time zone**, and voting preferences independently
   for each poll, use **Preview**, then **Enable**. Multiple polls can share a
   destination or use different destinations and posting times.
- **Send now...** and **Delete poll...** require confirmation and apply only to
   the selected poll. Deleting a poll removes its future scheduled job, not polls
   already posted to Telegram. All polls may be deleted; **New poll** remains
   available in the empty list.

Each poll posts to one destination. To send the same question to two destinations
or on two separate schedules, create two poll configurations. Different admins
can select and edit different polls without sharing a current selection.

### Forum Topics in Groups

1. In the desired group topic, send `/chatid@YourBotUsername` from an allowlisted
   admin account. The reply includes the group **Chat ID** and, when present,
   the **Topic ID** (`message_thread_id`).
2. Select the poll in `/admin`, then use **Destination > Enter chat ID** to
   set the group. Positive numeric chat IDs are converted to negative IDs;
   negative IDs remain unchanged and zero is rejected. This only adds the
   minus sign: it does not add a missing `100` prefix or reconstruct shortened
   IDs from Telegram links. Use the complete chat ID reported by the bot.
3. Use **Destination > Topic** and enter the positive topic ID. The bot checks
   that the selected group has forum topics enabled. Enter `0` to clear the
   topic and use the group's default destination (normally General).
4. Optionally use **Edit label** for a friendly name. Panels distinguish topics
   as `Studio > Topic 42`, and two topics in the same group remain separate
   reusable destinations. Selecting a saved destination copies its topic too.
5. Changing the topic disables this poll until you explicitly **Enable** it.
   **Preview** posts privately without a topic; **Send now...** and scheduled
   posting use the configured topic. Use a confirmed **Send now...** to check
   actual topic delivery before enabling the schedule.

Changing or re-entering the group ID, or selecting a forwarded channel post,
clears the previous topic. Topics cannot be configured for channels. The topic
must exist and be open, and the bot needs permission to send messages and polls
there. The bot does not create, reopen, or list topics. Existing saved polls
default to no topic, preserving their previous destination behavior.

#### Example: Send Polls to General

For Telegram's built-in **General** topic, suppose `/chatid@YourBotUsername`
reports chat ID `-1001234567890` (an illustrative ID; use your group's real ID):

1. Open a private chat with the bot, send `/admin`, and select your poll.
2. Choose **Destination > Enter chat ID** and enter `-1001234567890`.
   Entering `1001234567890` also works; the bot adds the minus sign.
3. Choose **Destination > Topic** and enter `0`. This clears the topic setting
   so Telegram uses the group's default **General** topic. The bot omits
   `message_thread_id` when sending; it does not send a literal topic ID of zero.
4. Choose **Destination > Edit label** and enter `Studio > General`.
   This is a display label, not a topic name lookup.
5. Return to the poll and confirm **Send now...** to verify it appears in
   General. Then **Enable** scheduled posting if desired. **Preview** only
   sends a private test poll, so it does not verify topic delivery.

This selects the existing General topic; it does not create one. General must
be available for posting. For a custom topic, even one named `general`, run
`/chatid@YourBotUsername` inside that topic and enter its positive **Topic ID**
instead of `0`. The **Topic** setting accepts numbers, not topic names.

### Upgrading from a Single Poll

Back up the active SQLite database before deploying this version. At first
startup, the previous single-poll configuration automatically becomes **Poll 1**,
preserving its question, answers, destination ID, schedule, time zone, and enabled
state. The migration is transactional and runs only once. Assign a destination
label through **Destination > Edit label** if the old poll has no saved title.

Use the existing named volume when recreating the container, or rebuild the image
and restart the Quadlet service. Do not start a second bot instance for migration.
To roll back to an older single-poll version, restore the pre-upgrade database
backup; old versions cannot manage the new multi-poll configuration. Open `/admin`
again after upgrading, since buttons from the old panel do not identify a poll.

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
characters). New group polls default to non-anonymous voting (**Anonymous: Off**),
so voters are visible. Multiple answers default to off. Channel polls are always
anonymous, as required by Telegram. Existing saved polls keep their voting
settings; use the **Anonymous** button to change an existing group poll.
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
formats, standard cron semantics, scheduler integration, multi-poll migration,
independent jobs, poll management, labelled destination selection, forum topic
routing, preview isolation, and chat ID sign normalization. They do not require
a token and do not send real Telegram messages.