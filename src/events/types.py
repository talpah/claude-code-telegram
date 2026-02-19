"""Concrete event types for the event bus."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bus import Event


@dataclass
class UserMessageEvent(Event):
    """A message from a Telegram user."""

    user_id: int = 0
    chat_id: int = 0
    text: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    source: str = "telegram"


@dataclass
class WebhookEvent(Event):
    """An external webhook delivery (GitHub, Notion, etc.)."""

    provider: str = ""
    event_type_name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    delivery_id: str = ""
    source: str = "webhook"


@dataclass
class ScheduledEvent(Event):
    """A cron/scheduled trigger."""

    job_id: str = ""
    job_name: str = ""
    prompt: str = ""
    working_directory: Path = field(default_factory=lambda: Path("."))
    target_chat_ids: list[int] = field(default_factory=list)
    skill_name: str | None = None
    source: str = "scheduler"


@dataclass
class AgentResponseEvent(Event):
    """An agent has produced a response to deliver."""

    chat_id: int = 0
    text: str = ""
    parse_mode: str | None = "HTML"
    reply_to_message_id: int | None = None
    source: str = "agent"
    originating_event_id: str | None = None
