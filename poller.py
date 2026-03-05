"""
Telegram long-poller.
Fetches updates via getUpdates (timeout=30), persists offset,
and emits (user_id, chat_id, first_name, text) to an asyncio Queue.
"""

import asyncio
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OFFSET_FILE = Path("/opt/rob-router/state/telegram_offset")
POLL_TIMEOUT = 30
RETRY_DELAY = 5


class TelegramPoller:
    def __init__(self, token: str, queue: asyncio.Queue):
        self.token = token
        self.queue = queue
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.offset = self._load_offset()
        self._running = False

    def _load_offset(self) -> int:
        try:
            if OFFSET_FILE.exists():
                return int(OFFSET_FILE.read_text().strip())
        except Exception:
            pass
        return 0

    def _save_offset(self, offset: int):
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(offset))

    async def run(self):
        self._running = True
        logger.info("Telegram poller started (offset=%d)", self.offset)

        async with httpx.AsyncClient(timeout=POLL_TIMEOUT + 10) as client:
            while self._running:
                try:
                    resp = await client.get(
                        f"{self.base_url}/getUpdates",
                        params={
                            "offset": self.offset,
                            "timeout": POLL_TIMEOUT,
                            "allowed_updates": ["message"],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if not data.get("ok"):
                        logger.warning("Telegram API not ok: %s", data)
                        await asyncio.sleep(RETRY_DELAY)
                        continue

                    updates = data.get("result", [])
                    for update in updates:
                        update_id = update["update_id"]
                        self.offset = update_id + 1
                        self._save_offset(self.offset)
                        await self._process_update(update)

                except httpx.ReadTimeout:
                    # Normal — long poll expired, just retry
                    continue
                except httpx.HTTPError as e:
                    logger.error("HTTP error polling Telegram: %s", e)
                    await asyncio.sleep(RETRY_DELAY)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("Unexpected error in poller: %s", e)
                    await asyncio.sleep(RETRY_DELAY)

        logger.info("Telegram poller stopped")

    async def _process_update(self, update: dict):
        """Extract message fields and enqueue text messages."""
        message = update.get("message")
        if not message:
            return

        # Skip non-text messages (photos, stickers, etc.)
        text = message.get("text")
        if not text:
            logger.debug("Skipping non-text message: %s", list(message.keys()))
            return

        user = message.get("from", {})
        user_id = user.get("id")
        first_name = user.get("first_name", "Unknown")
        chat_id = message.get("chat", {}).get("id")

        if not user_id or not chat_id:
            logger.warning("Missing user_id or chat_id in message: %s", message)
            return

        logger.info(
            "Message from %s (user_id=%s, chat_id=%s): %s",
            first_name,
            user_id,
            chat_id,
            text[:80],
        )
        await self.queue.put((user_id, chat_id, first_name, text))

    def stop(self):
        self._running = False
