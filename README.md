# Rob Router

Telegram → Claude Code router daemon. Routes Julian's Telegram messages to persistent Claude Code sessions.

## Architecture

```
Telegram ──► Poller ──► Queue ──► Router ──► Session Manager ──► claude subprocess(es)
                                                                        │
                                                              tg CLI (sends replies)
```

### Components

#### 1. `poller.py` — Telegram Poller
- Long-polls `getUpdates` (timeout=30s)
- Persists offset to `/opt/rob-router/state/telegram_offset`
- Emits `(user_id, chat_id, first_name, text)` tuples to an asyncio Queue
- Skips non-text messages (photos, stickers, etc.)

#### 2. `router.py` — Claude Router
- For each message, calls `claude -p '<routing prompt>' --output-format json --tools ""`
- Routing prompt includes the message + list of active sessions (name + description)
- Claude returns JSON: `{ chunks: [ { text, session_id, session_name } ] }`
- Routes each chunk to the appropriate session (creating new ones as needed)

#### 3. `session_manager.py` — Session Manager
- Each session = persistent subprocess: `claude --print --input-format stream-json --output-format stream-json --verbose --session-id <uuid> --allowedTools ...`
- Per-user isolation: each user_id has its own session namespace
- Sessions expire after 12h of inactivity (reaper runs every 5 minutes)
- Registry persisted to `/opt/rob-router/state/sessions.json`
- Each session writes a one-paragraph description to `/opt/rob-router/state/descs/<session_id>.txt`

#### 4. `scheduler.py` — Scheduler
- Uses APScheduler with CronTrigger (EST timezone)
- **morning-briefing**: 9am EST daily → synthetic message to `daily-assistant`
- **todo-checkin**: 12pm, 3pm, 7pm EST → synthetic message to `daily-assistant`
- `daily-assistant` is created at startup and exempt from the reaper

#### 5. Session System Prompt
Each session gets a system prompt instructing it to:
- Read Julian's profile from `~/memory/people/julian-moncarz.md`
- Use the `tg <chat_id> '[session-name] message'` CLI to send replies
- Write a one-paragraph description after each response to `/opt/rob-router/state/descs/<session_id>.txt`
- Stay concise (Telegram chat context)

## Setup

### Prerequisites
```bash
# Node.js + Claude CLI
npm install -g @anthropic-ai/claude-code

# Authenticate Claude (REQUIRED — Julian must do this once)
claude login
# Or: claude setup-token

# Python + uv
uv sync
```

### Environment
```bash
# .env symlink (already created)
ln -s /opt/rob-bot/.env /opt/rob-router/.env
```

Required env vars (in `/opt/rob-bot/.env`):
- `TELEGRAM_BOT_TOKEN` — Telegram bot token

**Do NOT set `ANTHROPIC_API_KEY`** — Claude CLI uses its own OAuth credentials from `~/.claude/` (set up via `claude login`).

### Start
```bash
cd /opt/rob-router
./start-router.sh
```

Monitor:
```bash
tmux attach -t rob-router
# or
tail -f /opt/rob-router/state/router.log
```

## State files
```
/opt/rob-router/state/
├── telegram_offset       # Last processed Telegram update ID
├── sessions.json         # Session registry (for crash recovery)
├── router.log            # Main log file
└── descs/
    └── <session_id>.txt  # Per-session descriptions (used by router)

/opt/rob-router/sessions/
└── <session_id>/
    └── prompt.md         # Session system prompt
```

## ⚠️ Authentication Required

Claude CLI must be authenticated before the router will work:
```bash
claude login
```

If Julian has a Pro/Max subscription, this will open a browser flow.
Alternatively: `claude setup-token` if a long-lived token is available.

**Status**: `claude auth status` returned `{"loggedIn": false}` at build time.
Julian needs to run `claude login` on the VPS once.

## Troubleshooting

**Router won't start**: Check `TELEGRAM_BOT_TOKEN` in `.env`

**Sessions not responding**: `claude auth status` — if not logged in, run `claude login`

**Stream-json format**: If sessions aren't receiving messages correctly, the stdin format
may need adjustment. The current format is:
```json
{"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "..."}]}}
```
Alternative formats to try if the above doesn't work:
- `{"role": "user", "content": "text"}` (plain text format)
- Just the raw text string (if stream-json input accepts it)

Check session subprocess stderr in the logs for format errors.

## GitHub
https://github.com/mr-robert-pi/rob-router
