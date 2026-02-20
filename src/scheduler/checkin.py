"""Proactive check-in service.

Periodically asks Claude whether to proactively reach out to the user
based on their active goals, time since last message, and time of day.
When Claude decides YES, publishes an AgentResponseEvent to the EventBus.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..events.bus import EventBus
from ..events.types import AgentResponseEvent
from ..storage.database import DatabaseManager

_STATE_FILE = Path.home() / ".claude-code-telegram" / "checkin_state.json"

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

Check-in archetypes (use as inspiration, adapt freely, or invent your own reason):
- Morning standup (8-10am): "What's the plan for today?"
- EOD wrap-up (5-7pm): "What did you ship today? Anything blocked?"
- Monday kickoff (Monday 8-10am): "What's the focus this week?"
- Friday retro (Friday 4-6pm): "What went well? What's carrying over to next week?"
- Stuck check (3+ hours silence, mid-day): "Still working on X? Hit a wall?"
- Long absence (24+ hours silence): "Back? Happy to pick up where we left off."
- Deadline nudge (goal mentions an approaching date): remind them
- Progress check (goal not mentioned in days): "Any movement on X?"
- Post-deploy follow-up (deploy/push in recent session, 30+ min silence): "Did it land okay?"
- Long session cooldown (heavy usage, extended silence after): "Good time for a break?"

Rules:
- Max {max_per_day} check-ins per day
- Only check in when there's a clear, concrete reason from the context above
- Never check in between {quiet_start}:00 and {quiet_end}:00 local time
- Don't be intrusive — skip if nothing fits naturally
- Keep messages short, casual, and specific to the user's actual situation

Respond with EXACTLY this format:
DECISION: YES or NO
MESSAGE: <your message to the user, if YES>
REASON: <which archetype or reason applied>"""


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
        state = self._load_state()
        self._checkin_count_today: int = state.get("count", 0)
        self._last_reset_date: str = state.get("date", "")

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

            tz_str = self.settings.user_timezone or "UTC"
            try:
                tz = ZoneInfo(tz_str)
            except ZoneInfoNotFoundError:
                logger.warning("Unknown user_timezone, falling back to UTC", timezone=tz_str)
                tz = ZoneInfo("UTC")
            now_local = now.astimezone(tz)

            today_str = now_local.strftime("%Y-%m-%d")
            if today_str != self._last_reset_date:
                self._checkin_count_today = 0
                self._last_reset_date = today_str
                self._save_state()

            quiet_start = self.settings.checkin_quiet_hours_start
            quiet_end = self.settings.checkin_quiet_hours_end
            hour = now_local.hour
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
                time=now_local.strftime("%H:%M"),
                day_of_week=now_local.strftime("%A"),
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
                self._save_state()

        except Exception as e:
            logger.error("Check-in evaluation failed", error=str(e))

    def _load_state(self) -> dict[str, Any]:
        if _STATE_FILE.exists():
            try:
                return json.loads(_STATE_FILE.read_text())
            except Exception:
                pass
        return {"date": "", "count": 0}

    def _save_state(self) -> None:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps({"date": self._last_reset_date, "count": self._checkin_count_today}, indent=2)
        )

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
