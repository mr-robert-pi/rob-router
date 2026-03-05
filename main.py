"""
Rob Router — main entry point.

Starts:
  1. Telegram poller (long-poll getUpdates)
  2. Router (Claude-powered message routing)
  3. Session manager (persistent claude subprocesses)
  4. Scheduler (morning briefings + to-do check-ins)
  5. Session reaper (cleans up inactive sessions every 5 min)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Load .env
_env_file = Path("/opt/rob-router/.env")
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key not in os.environ:  # don't overwrite existing env vars
            os.environ[key] = val

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/opt/rob-router/state/router.log"),
    ],
)
logger = logging.getLogger(__name__)

from poller import TelegramPoller
from router import Router
from scheduler import Scheduler
from session_manager import SessionManager

# Julian's Telegram info (from config)
JULIAN_USER_ID = 7579787534
JULIAN_CHAT_ID = 7579787534


async def message_worker(queue: asyncio.Queue, router: Router):
    """Process messages from the Telegram queue one at a time."""
    logger.info("Message worker started")
    while True:
        try:
            user_id, chat_id, first_name, text = await queue.get()
            logger.info("Processing message from %s: %s", first_name, text[:80])
            try:
                await router.route(user_id, chat_id, first_name, text)
            except Exception as e:
                logger.exception("Error routing message: %s", e)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Unexpected error in message worker: %s", e)


async def main():
    # Check required env vars
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set — check /opt/rob-router/.env")
        sys.exit(1)

    # Warn if ANTHROPIC_API_KEY is set (we don't want it polluting claude's auth)
    if os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY is set in environment. "
            "Router unsets it for claude subprocesses to force OAuth auth."
        )

    logger.info("=== Rob Router starting ===")

    # Check claude is available
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        logger.info("claude CLI: %s", stdout.decode().strip())
    except FileNotFoundError:
        logger.error("claude CLI not found! Install with: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    # Init components
    queue: asyncio.Queue = asyncio.Queue()
    session_manager = SessionManager()
    router = Router(session_manager)
    scheduler = Scheduler(
        session_manager=session_manager,
        julian_user_id=JULIAN_USER_ID,
        julian_chat_id=JULIAN_CHAT_ID,
    )
    poller = TelegramPoller(token=token, queue=queue)

    # Start scheduler (creates daily-assistant session)
    scheduler.start()

    # Run all tasks concurrently
    tasks = [
        asyncio.create_task(poller.run(), name="poller"),
        asyncio.create_task(message_worker(queue, router), name="worker"),
        asyncio.create_task(session_manager.reaper_loop(), name="reaper"),
    ]

    logger.info("Rob Router running. Ctrl+C to stop.")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down...")
        scheduler.stop()
        poller.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Kill all active sessions
        for session in list(session_manager._sessions.values()):
            await session_manager.kill_session(session)

        logger.info("Rob Router stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
