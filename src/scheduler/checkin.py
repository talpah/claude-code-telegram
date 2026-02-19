"""Proactive check-in service.

Periodically asks Claude whether to proactively reach out to the user
based on their active goals, time since last message, and time of day.
When Claude decides YES, publishes an AgentResponseEvent to the EventBus.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..events.bus import EventBus
from ..events.types import AgentResponseEvent
from ..storage.database import DatabaseManager

if TYPE_CHECKING:
    from ..config.settings import Settings

logger = structlog.get_logger()

_DECISION_PROMPT = """\
You are deciding whether to proactively check in with the user via Telegram.

Context:
- Current time: {time} ({day_of_week})
- Hours since last message: {hours:.1f}
- Active goals: {goals}
- Check-ins today: {count}

Rules:
- Max {max_per_day} check-ins per day
- Only check in for substantive reasons (deadlines, long silence, progress check)
- Never check in between {quiet_start}:00 and {quiet_end}:00 local time
- Don't be intrusive — only message if there's real value

Respond with EXACTLY this format:
DECISION: YES or NO
MESSAGE: <your message to the user, if YES>
REASON: <why you decided this>"""


class CheckInService:
    """Evaluate and send proactive check-ins via Claude + EventBus."""

    def __init__(
        self,
        claude_integration: Any,
        memory_manager: Any | None,
        event_bus: EventBus,
        db_manager: DatabaseManager,
        settings: "Settings",
    ) -> None:
        self.claude_integration = claude_integration
        self.memory_manager = memory_manager
        self.event_bus = event_bus
        self.db_manager = db_manager
        self.settings = settings
        self._checkin_count_today: int = 0
        self._last_reset_date: str = ""

    async def start(self, scheduler: AsyncIOScheduler) -> None:
        """Register the check-in evaluation job with an existing APScheduler instance."""
        interval = self.settings.checkin_interval_minutes
        scheduler.add_job(
            self.evaluate_checkin,
            trigger=IntervalTrigger(minutes=interval),
            id="checkin_evaluation",
            name="Proactive Check-in Evaluation",
            replace_existing=True,
        )
        logger.info("Check-in service registered", interval_minutes=interval)

    async def evaluate_checkin(self) -> None:
        """Evaluate whether to send a proactive check-in to the user."""
        try:
            now = datetime.now(UTC)

            today_str = now.strftime("%Y-%m-%d")
            if today_str != self._last_reset_date:
                self._checkin_count_today = 0
                self._last_reset_date = today_str

            quiet_start = self.settings.checkin_quiet_hours_start
            quiet_end = self.settings.checkin_quiet_hours_end
            hour = now.hour
            # Handle wrap-around (e.g. 22-8 spans midnight)
            if quiet_start > quiet_end:
                if hour >= quiet_start or hour < quiet_end:
                    return
            else:
                if quiet_start <= hour < quiet_end:
                    return

            if self._checkin_count_today >= self.settings.checkin_max_per_day:
                return

            chat_ids = self.settings.notification_chat_ids or []
            if not chat_ids:
                return

            hours_since = await self._hours_since_last_message()

            goals_str = "none"
            if self.memory_manager:
                user_id = chat_ids[0]
                goals = await self.memory_manager.get_active_goals(user_id)
                if goals:
                    goals_str = "; ".join(g.content[:60] for g in goals[:5])

            prompt = _DECISION_PROMPT.format(
                time=now.strftime("%H:%M"),
                day_of_week=now.strftime("%A"),
                hours=hours_since,
                goals=goals_str,
                count=self._checkin_count_today,
                max_per_day=self.settings.checkin_max_per_day,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )

            response_text = await self.claude_integration.quick_query(
                prompt=prompt,
                working_directory=self.settings.approved_directory,
            )

            decision, message = self._parse_decision(response_text)
            logger.info(
                "Check-in decision made",
                decision=decision,
                hours_since=hours_since,
                check_ins_today=self._checkin_count_today,
            )

            if decision and message:
                for chat_id in chat_ids:
                    event = AgentResponseEvent(chat_id=chat_id, text=message, parse_mode=None)
                    await self.event_bus.publish(event)
                self._checkin_count_today += 1

        except Exception as e:
            logger.error("Check-in evaluation failed", error=str(e))

    async def _hours_since_last_message(self) -> float:
        """Return hours elapsed since the most recent message in the DB."""
        try:
            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute("SELECT MAX(timestamp) FROM messages")
                row = await cursor.fetchone()
                if row and row[0]:
                    last_ts = row[0]
                    if isinstance(last_ts, str):
                        last_ts = datetime.fromisoformat(last_ts)
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=UTC)
                    return (datetime.now(UTC) - last_ts).total_seconds() / 3600
        except Exception as e:
            logger.warning("Failed to get last message time", error=str(e))
        return 24.0

    @staticmethod
    def _parse_decision(response_text: str) -> tuple[bool, str]:
        """Parse DECISION and MESSAGE lines from Claude's response."""
        decision = False
        message = ""
        for line in response_text.strip().splitlines():
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("DECISION:"):
                decision = stripped[9:].strip().upper() == "YES"
            elif upper.startswith("MESSAGE:"):
                message = stripped[8:].strip()
        return decision, message
