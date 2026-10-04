# AniWave Telegram Publishing Bot

Local Telegram automation for the **AniWave Database** forum supergroup. Uploads
posted by an administrator into a configured anime topic are validated, captioned
and published to the public channel **@aniwavebd**.

Runs entirely on your Windows machine with **long polling**. No web server, no
webhook, no Docker, no cloud database, no Redis, no paid hosting.

---

## 1. What it does

1. Watches **one** configured forum supergroup.
2. Accepts uploads **only** from the numeric IDs listed in `ADMIN_IDS`.
3. Accepts **only** these media types: video, animation, audio, photo, document,
   and supported media albums.
4. Maps the forum topic ID to anime metadata (One Piece / Naruto / Bleach).
5. Extracts the episode number from the caption (`EP 1165`, `Episode 25`,
   `12.5`, or just `1165`).
6. Generates the Bengali publication caption (plain text).
7. Publishes either after an admin presses **Publish**, or immediately when
   `AUTO_PUBLISH=true`.
8. Never publishes the same source message or album twice - across restarts,
   repeated updates, repeated button presses and retries.

Media is **copied** with Telegram's `copyMessage`, so files are reused from
Telegram's own storage. Nothing is downloaded and re-uploaded unless Telegram
itself requires it.

---

## 2. Publishing modes

### Approval mode (`AUTO_PUBLISH=false`, the default)

```
Admin uploads in a configured topic
        |
        v
Bot saves the record as "pending" and replies with a preview:
        [ Publish ]  [ Edit Episode ]  [ Cancel ]
        |
        +-- Publish  -> atomically claims the record and copies it
        +-- Edit     -> asks for a new episode number, updates the preview
        +-- Cancel   -> marks the record canceled
```

### Automatic mode (`AUTO_PUBLISH=true`)

```
Admin uploads in a configured topic
        |
        v
Bot validates the record and publishes immediately
        |
        +-- success -> confirmation posted back into the topic
        +-- failure -> record marked failed, use /retry
```

---

## 3. State machine

```
                    +-----------+
   (album part) --> | collecting|
                    +-----+-----+
                          |
                    collecting -> pending
                          |
          +---------------+----------------+
          |                                |
   pending -> publishing              pending -> canceled
          |                                |
  +-------+--------+               failed -> canceled
  |                |
publishing    publishing
  |                |
  v                v
published        failed  ---> (only when Telegram clearly sent nothing)
  |
  v
(terminal)

publishing --(process died mid-send)--> uncertain --> published  (admin: "already published")
                                                \-> failed    (admin: "not published", 2-step confirm)

Any state --(restart)--> unchanged.  A published record never becomes pending again.
```

**Important:** Telegram has no idempotency key for sends. If the process dies
while publishing, the post may or may not exist in the channel. Such records are
marked `uncertain` and are **never** retried automatically - an admin must
inspect the channel and resolve them with `/uncertain`.

---

## 4. Windows setup

### Prerequisites

- Python 3.11 or newer (developed and verified on **Python 3.14**)
- Git for Windows, if you want to use Git Bash

### Create a virtual environment

Git Bash:

```bash
cd /c/xampp/htdocs/aniwave
python -m venv .venv
source .venv/Scripts/activate
```

PowerShell:

```powershell
cd C:\xampp\htdocs\aniwave
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Command Prompt:

```bat
cd C:\xampp\htdocs\aniwave
python -m venv .venv
.venv\Scripts\activate.bat
```

### Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> `tzdata` is a hard requirement on Windows: Python ships no IANA timezone
> database there, and `TIMEZONE` is validated with `zoneinfo.ZoneInfo`.

### Create your `.env`

Git Bash / PowerShell:

```bash
cp .env.example .env
```

Command Prompt:

```bat
copy .env.example .env
```

Then edit `.env` and fill in real values. `.env` is git-ignored; never commit it.

---

## 5. Finding the IDs you need

| Value | How to get it |
|---|---|
| `SOURCE_GROUP_ID` | Add the bot to the group, send `/chatid` inside it. Supergroup ids start with `-100`. |
| Topic ids | Open the topic and send `/topicid` (or `/debug`). Use the reported **Message thread id**. |
| `ADMIN_IDS` | Your own numeric Telegram id. Send `/chatid` in private chat with the bot; `/chatid` prints "Your user id". |
| Bot token | `@BotFather` -> `/newbot` or `/token`. |

Example `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=replace_with_new_token
SOURCE_GROUP_ID=-1004428338491
TARGET_CHANNEL=@aniwavebd
ADMIN_IDS=6589890362
ONE_PIECE_TOPIC_ID=23
NARUTO_TOPIC_ID=6
BLEACH_TOPIC_ID=8
AUTO_PUBLISH=false
INCLUDE_HD_CLAIM=false
TIMEZONE=Asia/Dhaka
DATABASE_PATH=aniwave.db
LOG_LEVEL=INFO
```

### Adding a new anime, web series or movie

Topics are **configuration-driven**. Any environment variable ending in
`_TOPIC_ID` is registered automatically as a topic, and the topic title is
derived from the variable name:

| Variable | Resulting topic |
|---|---|
| `ONE_PIECE_TOPIC_ID` | One Piece (built-in emoji 📺) |
| `NARUTO_TOPIC_ID` | Naruto (built-in emoji 🍥) |
| `BLEACH_TOPIC_ID` | Bleach (built-in emoji ⚔️) |
| `WEB_SERIES_TOPIC_ID` | Web Series |
| `MOVIE_NAME_TOPIC_ID` | Movie Name |

So supporting a new section needs **one new line in `.env`** and no code change:

```dotenv
WEB_SERIES_TOPIC_ID=42
```

The three built-in anime variables are validated as required. To change which
ones are mandatory, edit `TOPIC_SPECS` in `config.py`.

---

## 6. Commands

All commands are **admin-only** and authorized by numeric id.

| Command | Purpose |
|---|---|
| `/start` | Overview and usage |
| `/help` | How to upload and publish |
| `/status` | Running state, configuration, database health, per-state counters |
| `/debug` | Safe diagnostics: chat id, topic id, user id, media type, mode |
| `/chatid` | Numeric chat id and your user id |
| `/topicid` | Numeric topic id for the current topic |
| `/retry [m\|a]<id>` | Retry a **failed** record (refuses published/uncertain/canceled) |
| `/uncertain` | List interrupted publications needing a manual decision |
| `/cancel` | Abandon an in-progress Edit Episode session |

---

## 7. Running

```bash
python bot.py
```

or double-click / run `start.bat`.

**Only one polling instance may run per token.** Telegram will reject a second
`getUpdates` from the same token.

---

## 8. Tests

Git Bash:

```bash
cd /c/xampp/htdocs/aniwave
python -m pytest tests -v
```

PowerShell / Command Prompt:

```powershell
cd C:\xampp\htdocs\aniwave
python -m pytest tests -v
```

Extra checks used during development:

```bash
python -m pytest --collect-only -v
python -m pytest tests -v -W error
python -m pytest tests --cov=. --cov-report=term-missing
python -m compileall .
python -c "import bot; print('import-ok')"
```

The suite performs **no** real Telegram API calls: the bot object is replaced
with a fake at the API boundary and every test uses a temporary SQLite file.

---

## 9. Security

- The bot token is read **only** from the environment / `.env`.
- `.env`, `.env.txt`, `*.db`, `*.db-wal`, `*.db-shm`, logs, caches, coverage data
  and virtual environments are git-ignored.
- A startup preflight scans the working tree for token-shaped strings and refuses
  to start if it finds one (only file locations are reported, never values).
- Log output passes through a redaction filter that scrubs the configured token
  and anything matching the token shape, including exception text.
- `/debug` and `/status` never print the token, the database path, or a traceback.
- Error notifications sent to admins contain the exception **type only**.

### If a token was ever exposed

1. Open `@BotFather` -> `/revoke` (or `/token`) for that bot and create a new one.
2. Put the **new** token in your local `.env`.
3. Delete any file the old token touched.
4. Re-run the tests: `python -m pytest tests -v`.

A revoked token is useless to whoever saw it; that is the only real fix.

---

## 10. Recovering an uncertain publication

1. The bot logs a warning at startup and posts a notice in the source group.
2. Send `/uncertain` to list the affected records.
3. For each record, **look at the channel**:
   - The post exists -> press **Already published**. The record becomes
     `published` and is never sent again.
   - The post does not exist -> press **Not published**, then confirm. The record
     becomes `failed` (single) or `pending` (album) and `/retry` can now send it.

The second confirmation exists because this action authorises another send.

---

## 11. Backing up the database

Stop the bot first (`Ctrl+C`), then:

Git Bash:

```bash
cd /c/xampp/htdocs/aniwave
cp aniwave.db "aniwave-backup-$(date +%Y%m%d-%H%M%S).db"
```

PowerShell:

```powershell
cd C:\xampp\htdocs\aniwave
Copy-Item aniwave.db "aniwave-backup-$(Get-Date -Format yyyyMMdd-HHmmss).db"
```

Also copy `-wal` / `-shm` files if they exist, or run
`sqlite3 aniwave.db ".backup backup.db"` while the bot is stopped.

Migrations are additive and idempotent: an existing database keeps all of its
rows and simply gains columns/tables.

---

## 12. Troubleshooting

**Zero tests collected**
Make sure you run from the project root and that `pytest.ini` has
`testpaths = tests`. Verify with `python -m pytest --collect-only -v`. Avoid
unquoted Windows backslash paths in Git Bash - use `/c/xampp/htdocs/aniwave/tests`.

**`Unauthorized` / `InvalidToken`**
The token is wrong or revoked. Generate a new one with `@BotFather` and update
`.env`. Run `python bot.py` and read the first log lines.

**`Forbidden: bot is not a member of the channel`**
Add the bot to `@aniwavebd` as an **administrator** with permission to post
messages.

**Bot has no permission in the source group**
Add it to the supergroup. To receive forum-topic updates it needs to be an
admin. Enable "Manage Topics" if topic filtering misbehaves.

**Uploads are ignored**
Check each condition in `/debug` inside the topic: chat id matches
`SOURCE_GROUP_ID`, the topic id is configured, your numeric id is in
`ADMIN_IDS`, and the message really contains supported media (an edit does not
count - edited messages are ignored by design).

**"no episode number"**
The caption did not contain a recognisable episode. Accepted forms:
`EP 1165`, `Episode 25`, `12.5`, `1165`. Press **Edit Episode** to set it
manually, or fix the caption and use `/retry`.

**Album rejected**
Telegram only allows photos/videos to mix freely in one album; every other type
must be homogeneous, and a media group holds 2-10 items. Mixed albums are refused
before anything is sent.

**`database is locked`**
Only one process may write. Make sure a second `python bot.py` (or an old
window) is not still running. WAL mode plus a 10 s busy timeout is configured; a
lock that persists usually means another instance holds the file.

**Two posts appeared for one episode**
That should be impossible through the bot. Check `/uncertain` output and the log
for `publish-uncertain` lines, which indicate a network timeout where delivery
was unknown. Resolve those records manually rather than retrying blindly.

---

## 13. Project layout

```
aniwave/
├── bot.py            entry point, lifecycle, error handler, redacting logging
├── config.py         strict env validation, topic registry, secret scanning
├── caption.py        episode parsing/validation + plain-text caption builder
├── database.py       SQLite schema v2, migrations, CAS transitions, recovery
├── publisher.py      Telegram sends, input-media classes, clear vs uncertain
├── albums.py         durable album collection, quiet period, restart recovery
├── ui.py             callback encoding, keyboards, admin-facing text
├── handlers.py       commands, media intake, callbacks, Edit conversation
├── tests/            pytest suite (no network, temporary databases)
├── requirements.txt  pinned dependencies
├── .env.example      sanitised template
└── start.bat         Windows launcher
```