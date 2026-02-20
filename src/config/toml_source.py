"""Custom pydantic-settings source for a sectioned TOML configuration file.

The TOML file organises settings into labelled sections (e.g. [claude],
[features]) purely for readability. This source flattens all sections into a
single dict keyed by pydantic field name, which is what pydantic-settings
expects from ``__call__()``.

Section → field-name mapping (``SECTION_MAP``) is the single source of truth.
The inverse (``FIELD_TO_SECTION``) is derived at module load time and used by
``settings_ui.py`` when writing individual field values back to the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomlkit
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from src.utils.constants import APP_HOME

# Default path — can be overridden per-instance or patched in tests.
TOML_PATH: Path = APP_HOME / "config" / "settings.toml"

# ── Section → field-name mapping ─────────────────────────────────────────────

SECTION_MAP: dict[str, list[str]] = {
    "required": [
        "telegram_bot_token",
        "telegram_bot_username",
        "approved_directory",
        "allowed_users",
    ],
    "auth": [
        "enable_token_auth",
        "auth_token_secret",
    ],
    "claude": [
        "anthropic_api_key",
        "claude_binary_path",
        "claude_cli_path",
        "claude_model",
        "claude_max_turns",
        "claude_timeout_seconds",
        "claude_max_cost_per_user",
        "claude_allowed_tools",
        "claude_disallowed_tools",
    ],
    "sandbox": [
        "sandbox_enabled",
        "sandbox_excluded_commands",
    ],
    "security": [
        "disable_security_patterns",
        "disable_tool_validation",
    ],
    "rate_limit": [
        "rate_limit_requests",
        "rate_limit_window",
        "rate_limit_burst",
    ],
    "storage": [
        "database_url",
        "session_timeout_hours",
        "session_timeout_minutes",
        "max_sessions_per_user",
    ],
    "features": [
        "enable_mcp",
        "mcp_config_path",
        "enable_git_integration",
        "enable_file_uploads",
        "enable_quick_actions",
        "agentic_mode",
    ],
    "output": [
        "verbose_level",
    ],
    "monitoring": [
        "log_level",
        "enable_telemetry",
        "sentry_dsn",
    ],
    "development": [
        "debug",
        "development_mode",
    ],
    "webhook": [
        "webhook_url",
        "webhook_port",
        "webhook_path",
        "enable_api_server",
        "api_server_port",
        "enable_scheduler",
        "github_webhook_secret",
        "webhook_api_secret",
        "notification_chat_ids",
    ],
    "projects": [
        "enable_project_threads",
        "project_threads_mode",
        "project_threads_chat_id",
        "projects_config_path",
    ],
    "personalization": [
        "user_profile_path",
        "user_name",
        "user_timezone",
    ],
    "voice": [
        "voice_provider",
        "groq_api_key",
        "whisper_binary",
        "whisper_model_path",
    ],
    "memory": [
        "enable_memory",
        "enable_memory_embeddings",
        "memory_max_facts",
        "memory_max_context_items",
    ],
    "checkins": [
        "enable_checkins",
        "checkin_interval_minutes",
        "checkin_max_per_day",
        "checkin_quiet_hours_start",
        "checkin_quiet_hours_end",
    ],
}

# Inverse lookup: field_name → section_name
FIELD_TO_SECTION: dict[str, str] = {field: section for section, fields in SECTION_MAP.items() for field in fields}


# ── Source class ──────────────────────────────────────────────────────────────


class TomlSettingsSource(PydanticBaseSettingsSource):
    """Flatten a sectioned ``settings.toml`` into a pydantic-settings source.

    Missing file → returns empty dict (graceful no-op).
    Empty-string values for optional fields → omitted (let pydantic default kick in).
    tomlkit wrapper objects → unwrapped to plain Python types before returning.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        toml_path: Path | None = None,
    ) -> None:
        super().__init__(settings_cls)
        # Reference module-level TOML_PATH at call time so tests can monkeypatch it.
        self._toml_path = toml_path if toml_path is not None else TOML_PATH
        self._data: dict[str, Any] = self._load()

    # -- Internal ─────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        if not self._toml_path.exists():
            return {}

        text = self._toml_path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)

        flat: dict[str, Any] = {}
        for section, field_names in SECTION_MAP.items():
            table = doc.get(section)
            if table is None:
                continue
            for field_name in field_names:
                if field_name not in table:
                    continue
                raw = _unwrap(table[field_name])
                # Drop empty strings for optional fields so pydantic's None default wins
                if raw == "" or raw == []:
                    continue
                flat[field_name] = raw

        return flat

    # -- pydantic-settings interface ──────────────────────────────────────────

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        value = self._data.get(field_name)
        return value, field_name, isinstance(value, (dict, list))

    def __call__(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            value, key, _ = self.get_field_value(self.settings_cls.model_fields[field_name], field_name)
            if value is not None:
                result[key] = value
        return result


# ── Helpers ───────────────────────────────────────────────────────────────────


def _unwrap(val: Any) -> Any:
    """Convert tomlkit wrapper objects to plain Python types."""
    if hasattr(val, "unwrap"):
        return val.unwrap()
    if isinstance(val, list):
        return [_unwrap(v) for v in val]
    return val
