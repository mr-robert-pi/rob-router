"""
Session manager for Claude Code sessions using --resume.

Instead of keeping persistent subprocesses alive, each message spawns a fresh
`claude -p` call with `--resume <session-id>`. Claude Code persists conversation
state on disk automatically — no stdin/stdout juggling required.

First message in a session:
  claude -p '<text>' --session-id <uuid> --system-prompt <prompt.md>
          --output-format stream-json --dangerously-skip-permissions
          --allowedTools <tools>

Subsequent messages:
  claude -p '<text>' --resume <uuid>
          --output-format stream-json --dangerously-skip-permissions
          --allowedTools <tools>

stdout stream-json events (read line by line):
  {"type": "assistant", "message": {"role": "assistant", "content": [...]}}
  {"type": "result", "subtype": "success", "result": "...", "cost_usd": ...}
  {"type": "result", "subtype": "error_during_execution", ...}
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = Path("/opt/rob-router/state/sessions.json")
DESCS_DIR = Path("/opt/rob-router/state/descs")
SESSIONS_DIR = Path("/opt/rob-router/sessions")
SESSION_TTL = 12 * 3600  # 12 hours
REAPER_INTERVAL = 300    # 5 minutes

ALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch"
)

SYSTEM_PROMPT_TEMPLATE = """\
You are Rob's AI assistant, running as session "{session_name}" for Julian Moncarz.

## Your identity
Session name: {session_name}
Initial focus: {focus}

## Julian's profile
Read ~/memory/people/julian-moncarz.md for full context on Julian, his goals, and his life.

## Key memory paths
- ~/memory/todo-julian.md — Julian's personal to-do list
- ~/memory/todo-rob.md — Tasks for Rob (the AI system)
- ~/memory/journal/ — Journal entries

## Available CLIs
Read ~/memory/tools/cli.md for full CLI documentation. Key tools:
- tg <chat_id> 'message' — send Telegram message (ALWAYS use single quotes)
- search "query" — web search
- gmail inbox / gmail send — email
- teammate <name> "task" — spawn a research agent

## Responding
- Keep responses SHORT and conversational (this is Telegram chat).
- Send your response via: tg {chat_id} '[{session_name}] your message here'
- For long responses (>4000 chars), split at sentence boundaries into multiple tg calls.
- ALWAYS use single quotes around the tg message argument.

## After each response
Write a one-paragraph description of what you're doing / what this session is about to:
  /opt/rob-router/state/descs/{session_id}.txt
This is used by the router to route future messages to the right session.

## Permissions
You have full tool access. Act autonomously — don't ask for confirmation on routine tasks.
"""


class Session:
    def __init__(
        self,
        session_id: str,
        user_id: int,
        session_name: str,
        chat_id: int,
        focus: str,
        system_prompt: str,
        exempt_from_reaper: bool = False,
        started: bool = False,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.session_name = session_name
        self.chat_id = chat_id
        self.focus = focus
        self.system_prompt = system_prompt
        self.exempt_from_reaper = exempt_from_reaper
        self.last_activity = time.time()
        # Has Claude Code ever been called with --session-id for this session?
        # If True, subsequent calls use --resume. If False, first call uses --session-id.
        self.started = started
        self._lock = asyncio.Lock()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "session_name": self.session_name,
            "chat_id": self.chat_id,
            "focus": self.focus,
            "system_prompt": self.system_prompt,
            "exempt_from_reaper": self.exempt_from_reaper,
            "last_activity": self.last_activity,
            "started": self.started,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        s = cls(
            session_id=d["session_id"],
            user_id=d["user_id"],
            session_name=d["session_name"],
            chat_id=d["chat_id"],
            focus=d.get("focus", ""),
            system_prompt=d["system_prompt"],
            exempt_from_reaper=d.get("exempt_from_reaper", False),
            started=d.get("started", False),
        )
        s.last_activity = d.get("last_activity", time.time())
        return s


class SessionManager:
    def __init__(self):
        # Registry: session_id -> Session
        self._sessions: dict[str, Session] = {}
        # Index: (user_id, session_name) -> session_id
        self._name_index: dict[tuple[int, str], str] = {}
        self._load_state()

    # ── State persistence ──────────────────────────────────────────────────

    def _load_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DESCS_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            for d in data.get("sessions", []):
                s = Session.from_dict(d)
                self._sessions[s.session_id] = s
                self._name_index[(s.user_id, s.session_name)] = s.session_id
            logger.info("Loaded %d sessions from state", len(self._sessions))
        except Exception as e:
            logger.warning("Could not load session state: %s", e)

    def _save_state(self):
        try:
            data = {"sessions": [s.to_dict() for s in self._sessions.values()]}
            STATE_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error("Could not save session state: %s", e)

    # ── Session lifecycle ──────────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def get_session_by_name(self, user_id: int, session_name: str) -> Optional[Session]:
        sid = self._name_index.get((user_id, session_name))
        if sid:
            return self._sessions.get(sid)
        return None

    def list_sessions(self, user_id: int) -> list[Session]:
        return [
            s for s in self._sessions.values()
            if s.user_id == user_id
        ]

    def create_session(
        self,
        user_id: int,
        session_name: str,
        chat_id: int,
        focus: str,
        exempt_from_reaper: bool = False,
    ) -> Session:
        session_id = str(uuid.uuid4())
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            session_name=session_name,
            focus=focus,
            chat_id=chat_id,
            session_id=session_id,
        )
        # Save prompt to file (used on first call)
        prompt_dir = SESSIONS_DIR / session_id
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / "prompt.md").write_text(system_prompt)

        session = Session(
            session_id=session_id,
            user_id=user_id,
            session_name=session_name,
            chat_id=chat_id,
            focus=focus,
            system_prompt=system_prompt,
            exempt_from_reaper=exempt_from_reaper,
            started=False,
        )
        self._sessions[session_id] = session
        self._name_index[(user_id, session_name)] = session_id
        self._save_state()
        logger.info("Created session %s (%s) for user %s", session_id, session_name, user_id)
        return session

    def kill_session(self, session: Session):
        """Remove a session from the registry (no process to terminate)."""
        logger.info("Removing session %s (%s)", session.session_id, session.session_name)

    async def send_message(self, session: Session, text: str) -> str:
        """
        Send a message to a Claude Code session and collect the response.

        Spawns a fresh `claude -p` subprocess with --resume (or --session-id on
        first call). Returns the raw stream-json output for logging. The session
        itself sends the Telegram reply via the tg CLI as instructed in its
        system prompt.
        """
        async with session._lock:
            session.last_activity = time.time()

            # Build command
            prompt_file = SESSIONS_DIR / session.session_id / "prompt.md"

            if not session.started:
                # First message: start a new session with the given UUID and system prompt
                cmd = [
                    "claude",
                    "--print",
                    "--session-id", session.session_id,
                    "--system-prompt", str(prompt_file),
                    "--output-format", "stream-json",
                    "--allowedTools", ALLOWED_TOOLS,
                    "--dangerously-skip-permissions",
                    text,
                ]
            else:
                # Subsequent messages: resume the existing session
                cmd = [
                    "claude",
                    "--print",
                    "--resume", session.session_id,
                    "--output-format", "stream-json",
                    "--allowedTools", ALLOWED_TOOLS,
                    "--dangerously-skip-permissions",
                    text,
                ]

            # Environment: inherit everything except ANTHROPIC_API_KEY
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

            logger.info(
                "Sending to session %s (%s): %s",
                session.session_id,
                session.session_name,
                text[:80],
            )

            output_lines = []
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd="/opt/rob-router",
                )

                # Mark as started so future calls use --resume
                session.started = True
                self._save_state()

                # Read stream-json events line by line until 'result'
                try:
                    while True:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=300,  # 5 min max per turn
                        )
                        if not line:
                            logger.warning(
                                "Session %s stdout EOF before result event",
                                session.session_id,
                            )
                            break

                        line_str = line.decode().strip()
                        if not line_str:
                            continue

                        output_lines.append(line_str)

                        try:
                            event = json.loads(line_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type")
                        logger.debug("Session %s event: %s", session.session_id, event_type)

                        if event_type == "result":
                            subtype = event.get("subtype", "")
                            if subtype == "error_during_execution":
                                logger.error(
                                    "Session %s error: %s",
                                    session.session_id,
                                    event.get("result", "unknown error"),
                                )
                            # Drain remaining stdout (shouldn't be any, but clean up)
                            break

                except asyncio.TimeoutError:
                    logger.error(
                        "Session %s timed out waiting for response",
                        session.session_id,
                    )
                    proc.kill()
                    return "[timeout waiting for response]"

                # Wait for process to exit (it should have already)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()

                # Log any stderr
                stderr_data = await proc.stderr.read()
                if stderr_data:
                    for stderr_line in stderr_data.decode().splitlines():
                        if stderr_line.strip():
                            logger.debug(
                                "[%s stderr] %s", session.session_name, stderr_line
                            )

            except Exception as e:
                logger.error(
                    "Error running claude for session %s: %s", session.session_id, e
                )
                return f"[session error: {e}]"

            return "\n".join(output_lines)

    # ── Session description ────────────────────────────────────────────────

    def get_description(self, session_id: str) -> str:
        desc_file = DESCS_DIR / f"{session_id}.txt"
        try:
            if desc_file.exists():
                return desc_file.read_text().strip()
        except Exception:
            pass
        return ""

    # ── Reaper ────────────────────────────────────────────────────────────

    async def reaper_loop(self):
        """Remove sessions inactive for more than SESSION_TTL. Runs every 5 min."""
        logger.info("Session reaper started (TTL=%dh)", SESSION_TTL // 3600)
        while True:
            await asyncio.sleep(REAPER_INTERVAL)
            await self._reap()

    async def _reap(self):
        now = time.time()
        to_kill = [
            s for s in list(self._sessions.values())
            if not s.exempt_from_reaper
            and (now - s.last_activity) > SESSION_TTL
        ]
        for session in to_kill:
            logger.info(
                "Reaping inactive session %s (%s) — inactive for %.1fh",
                session.session_id,
                session.session_name,
                (now - session.last_activity) / 3600,
            )
            self.kill_session(session)
            del self._sessions[session.session_id]
            key = (session.user_id, session.session_name)
            if self._name_index.get(key) == session.session_id:
                del self._name_index[key]
        if to_kill:
            self._save_state()
