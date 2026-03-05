"""
Message router.

For each incoming Telegram message, calls:
  claude -p '<routing prompt>' --output-format json --tools "" --no-session-persistence

The routing prompt includes the message text and a list of active sessions
(name + description). Claude returns JSON:
  {
    "chunks": [
      {
        "text": "...",
        "session_id": "<existing UUID or 'NEW'>",
        "session_name": "<slug if NEW, e.g. 'taisi-funding'>"
      }
    ]
  }

Multiple chunks allow splitting a message across sessions (e.g. "check on my
TAISI funding AND do a UofT course lookup").
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Optional

from session_manager import Session, SessionManager

logger = logging.getLogger(__name__)

ROUTING_PROMPT_TEMPLATE = """\
You are a message router for Julian Moncarz's AI assistant system.

Julian sent this Telegram message:
<message>{message}</message>

Active sessions:
{sessions_list}

Your job: decide which session(s) should handle this message, and what text to send each session.

Rules:
1. Route to an existing session if the message is clearly related to its current work.
2. If no session fits, route to NEW with a descriptive slug (e.g. "uoft-courses", "taisi-funding", "workout-plan").
3. You can split a message into multiple chunks for different sessions.
4. The 'daily-assistant' session handles general requests, scheduling, to-dos.
5. Keep the chunk text faithful to the original message — don't rewrite it heavily.
6. Session slugs: lowercase, hyphens only, max 30 chars.

Respond with ONLY valid JSON, no markdown, no explanation:
{{
  "chunks": [
    {{
      "text": "<text to send to this session>",
      "session_id": "<existing session UUID or 'NEW'>",
      "session_name": "<slug — required if session_id is 'NEW', ignored otherwise>"
    }}
  ]
}}
"""

FALLBACK_SESSION_NAME = "daily-assistant"
FALLBACK_FOCUS = "General assistant — handles daily to-dos, briefings, and miscellaneous requests for Julian."


class Router:
    def __init__(self, session_manager: SessionManager):
        self.sm = session_manager

    async def route(self, user_id: int, chat_id: int, first_name: str, text: str):
        """Route an incoming message to the appropriate session(s)."""
        # Build the sessions list for the prompt
        sessions = self.sm.list_sessions(user_id)
        sessions_list = self._format_sessions(sessions)

        # Get routing decision from Claude
        chunks = await self._call_router(text, sessions_list)
        if not chunks:
            # Fallback: route to daily-assistant
            chunks = [{"text": text, "session_id": "NEW", "session_name": FALLBACK_SESSION_NAME}]

        logger.info("Router decision for '%s': %d chunk(s)", text[:50], len(chunks))

        # Process each chunk
        for chunk in chunks:
            await self._dispatch_chunk(chunk, user_id, chat_id, first_name)

    def _format_sessions(self, sessions: list[Session]) -> str:
        if not sessions:
            return "(no active sessions)"
        lines = []
        for s in sessions:
            desc = self.sm.get_description(s.session_id)
            desc_line = f"  Description: {desc}" if desc else "  Description: (none yet)"
            lines.append(
                f"- session_id: {s.session_id}\n"
                f"  name: {s.session_name}\n"
                f"{desc_line}"
            )
        return "\n".join(lines)

    async def _call_router(self, message: str, sessions_list: str) -> Optional[list[dict]]:
        """Call claude CLI for routing decision. Returns list of chunk dicts."""
        prompt = ROUTING_PROMPT_TEMPLATE.format(
            message=message,
            sessions_list=sessions_list,
        )

        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "json",
            "--tools", "",                   # disable all tools
            "--no-session-persistence",      # don't save routing calls to history
        ]

        # No ANTHROPIC_API_KEY — use claude's own OAuth credentials
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd="/opt/rob-router",
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

            if proc.returncode != 0:
                logger.error(
                    "Router claude call failed (rc=%d): %s",
                    proc.returncode,
                    stderr.decode()[:500],
                )
                return None

            output = stdout.decode().strip()
            logger.debug("Router raw output: %s", output[:500])

            # Parse JSON response
            return self._parse_routing_response(output)

        except asyncio.TimeoutError:
            logger.error("Router claude call timed out")
            return None
        except Exception as e:
            logger.exception("Router claude call error: %s", e)
            return None

    def _parse_routing_response(self, output: str) -> Optional[list[dict]]:
        """Parse claude's JSON routing response."""
        # claude --output-format json returns a JSON object with 'result' field
        # which contains the text response (which itself should be JSON)
        try:
            outer = json.loads(output)
            # The actual response text is in outer['result']
            result_text = outer.get("result", output)
        except json.JSONDecodeError:
            result_text = output

        # Strip markdown fences if present
        result_text = re.sub(r"^```(?:json)?\s*", "", result_text.strip())
        result_text = re.sub(r"\s*```$", "", result_text.strip())

        try:
            parsed = json.loads(result_text)
            chunks = parsed.get("chunks", [])
            if not isinstance(chunks, list):
                logger.error("Router: 'chunks' is not a list: %s", parsed)
                return None
            return chunks
        except json.JSONDecodeError as e:
            logger.error("Router: could not parse routing JSON: %s\nOutput: %s", e, result_text[:300])
            return None

    async def _dispatch_chunk(self, chunk: dict, user_id: int, chat_id: int, first_name: str):
        """Send a chunk to its target session, creating the session if needed."""
        session_id = chunk.get("session_id", "NEW")
        session_name = chunk.get("session_name", FALLBACK_SESSION_NAME)
        text = chunk.get("text", "")

        if not text:
            logger.warning("Empty text chunk, skipping")
            return

        # Find or create session
        if session_id == "NEW":
            # Check if a session with this name already exists
            session = self.sm.get_session_by_name(user_id, session_name)
            if session is None:
                focus = f"Started by {first_name}: {text[:100]}"
                session = self.sm.create_session(
                    user_id=user_id,
                    session_name=session_name,
                    chat_id=chat_id,
                    focus=focus,
                    exempt_from_reaper=(session_name == FALLBACK_SESSION_NAME),
                )
        else:
            session = self.sm.get_session(session_id)
            if session is None:
                logger.warning("Session %s not found, creating new one", session_id)
                session = self.sm.create_session(
                    user_id=user_id,
                    session_name=session_name or FALLBACK_SESSION_NAME,
                    chat_id=chat_id,
                    focus=text[:100],
                )

        logger.info(
            "Dispatching to session %s (%s): %s",
            session.session_id,
            session.session_name,
            text[:60],
        )

        # Send message to session
        await self.sm.send_message(session, text)
