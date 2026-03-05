"""
Scheduler using APScheduler with CronTrigger.

Jobs:
  - morning-briefing: 9am EST daily → synthetic message to 'daily-assistant'
  - todo-checkin:    12pm, 3pm, 7pm EST → synthetic message to 'daily-assistant'

The 'daily-assistant' session is created at startup and exempt from the 12h reaper.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from session_manager import SessionManager

logger = logging.getLogger(__name__)

EST = pytz.timezone("America/New_York")
DAILY_SESSION_NAME = "daily-assistant"
DAILY_FOCUS = (
    "Julian's daily assistant. Handles morning briefings, to-do check-ins, "
    "reminders, and general requests. Always on."
)


class Scheduler:
    def __init__(self, session_manager: SessionManager, julian_user_id: int, julian_chat_id: int):
        self.sm = session_manager
        self.user_id = julian_user_id
        self.chat_id = julian_chat_id
        self._scheduler = AsyncIOScheduler(timezone=EST)
        self._daily_session = None

    def ensure_daily_session(self):
        """Create the daily-assistant session if it doesn't exist."""
        session = self.sm.get_session_by_name(self.user_id, DAILY_SESSION_NAME)
        if session is None:
            session = self.sm.create_session(
                user_id=self.user_id,
                session_name=DAILY_SESSION_NAME,
                chat_id=self.chat_id,
                focus=DAILY_FOCUS,
                exempt_from_reaper=True,
            )
            logger.info("Created daily-assistant session %s", session.session_id)
        else:
            logger.info("Found existing daily-assistant session %s", session.session_id)
        self._daily_session = session
        return session

    def start(self):
        """Register all jobs and start the scheduler."""
        self.ensure_daily_session()

        # Morning briefing: 9am EST daily
        self._scheduler.add_job(
            self._morning_briefing,
            CronTrigger(hour=9, minute=0, timezone=EST),
            id="morning-briefing",
            name="Morning Briefing",
            replace_existing=True,
        )

        # Todo check-in: 12pm, 3pm, 7pm EST
        self._scheduler.add_job(
            self._todo_checkin,
            CronTrigger(hour="12,15,19", minute=0, timezone=EST),
            id="todo-checkin",
            name="Todo Check-in",
            replace_existing=True,
        )

        self._scheduler.start()
        logger.info("Scheduler started (morning briefing 9am EST, check-ins 12/3/7pm EST)")

    def stop(self):
        self._scheduler.shutdown(wait=False)

    async def _get_daily_session(self):
        """Get or recreate the daily-assistant session."""
        if self._daily_session is None:
            self._daily_session = self.ensure_daily_session()
        # Re-fetch in case it was recreated
        session = self.sm.get_session_by_name(self.user_id, DAILY_SESSION_NAME)
        if session is None:
            session = self.ensure_daily_session()
        return session

    async def _morning_briefing(self):
        logger.info("Running morning briefing job")
        session = await self._get_daily_session()
        message = (
            "Good morning Julian! Time for your daily briefing. "
            "Please:\n"
            "1. Check ~/memory/todo-julian.md and summarise what's on the plate today.\n"
            "2. Check ~/memory/journal/ for any recent context.\n"
            "3. Give a brief, actionable summary — what should he focus on today?\n"
            "Send the briefing via tg."
        )
        await self.sm.send_message(session, message)

    async def _todo_checkin(self):
        logger.info("Running todo check-in job")
        session = await self._get_daily_session()
        message = (
            "Quick to-do check-in for Julian. "
            "Look at ~/memory/todo-julian.md — anything urgent or time-sensitive right now? "
            "If so, send a brief nudge via tg. If everything looks fine, no need to message."
        )
        await self.sm.send_message(session, message)
