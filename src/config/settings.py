"""Configuration management using Pydantic Settings.

Features:
- Environment variable loading
- Type validation
- Default values
- Computed properties
- Environment-specific settings
"""

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from src.utils.constants import (
    DEFAULT_CLAUDE_MAX_COST_PER_USER,
    DEFAULT_CLAUDE_MAX_TURNS,
    DEFAULT_CLAUDE_TIMEOUT_SECONDS,
    DEFAULT_DATABASE_URL,
    DEFAULT_MAX_SESSIONS_PER_USER,
    DEFAULT_RATE_LIMIT_BURST,
    DEFAULT_RATE_LIMIT_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW,
    DEFAULT_SESSION_TIMEOUT_HOURS,
)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Bot settings
    telegram_bot_token: SecretStr = Field(..., description="Telegram bot token from BotFather")
    telegram_bot_username: str = Field(..., description="Bot username without @")

    # Security
    approved_directory: Path = Field(
        default_factory=lambda: Path.home() / ".claude-code-telegram",
        description="Primary workspace directory (default: ~/.claude-code-telegram)",
    )
    allowed_paths: list[Path] = Field(
        default_factory=list,
        description="Additional directories where Claude can read/write",
    )
    allowed_users: list[int] | None = Field(None, description="Allowed Telegram user IDs")
    enable_token_auth: bool = Field(False, description="Enable token-based authentication")
    auth_token_secret: SecretStr | None = Field(None, description="Secret for auth tokens")

    # Security relaxation (for trusted environments)
    disable_security_patterns: bool = Field(
        False,
        description=("Disable dangerous pattern validation (pipes, redirections, etc.)"),
    )
    disable_tool_validation: bool = Field(
        False,
        description="Allow all Claude tools by bypassing tool validation checks",
    )

    # Claude settings
    claude_binary_path: str | None = Field(None, description="Path to Claude CLI binary (deprecated)")
    claude_cli_path: str | None = Field(None, description="Path to Claude CLI executable")
    anthropic_api_key: SecretStr | None = Field(
        None,
        description="Anthropic API key for SDK (optional if CLI logged in)",
    )
    claude_model: str = Field("claude-sonnet-4-5", description="Claude model to use")
    claude_max_turns: int = Field(DEFAULT_CLAUDE_MAX_TURNS, description="Max conversation turns")
    claude_timeout_seconds: int = Field(DEFAULT_CLAUDE_TIMEOUT_SECONDS, description="Claude timeout")
    claude_max_cost_per_user: float = Field(DEFAULT_CLAUDE_MAX_COST_PER_USER, description="Max cost per user")
    claude_allowed_tools: list[str] | None = Field(
        default=[
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "LS",
            "Task",
            "MultiEdit",
            "NotebookRead",
            "NotebookEdit",
            "WebFetch",
            "TodoRead",
            "TodoWrite",
            "WebSearch",
        ],
        description="List of allowed Claude tools",
    )
    claude_disallowed_tools: list[str] | None = Field(
        default=[],
        description="List of explicitly disallowed Claude tools/commands",
    )

    # Sandbox settings
    sandbox_enabled: bool = Field(
        True,
        description="Enable OS-level bash sandboxing for approved dir",
    )
    sandbox_excluded_commands: list[str] | None = Field(
        default=["git", "npm", "pip", "poetry", "make", "docker", "uv"],
        description="Commands that run outside the sandbox (need system access)",
    )

    # Rate limiting
    rate_limit_requests: int = Field(DEFAULT_RATE_LIMIT_REQUESTS, description="Requests per window")
    rate_limit_window: int = Field(DEFAULT_RATE_LIMIT_WINDOW, description="Rate limit window seconds")
    rate_limit_burst: int = Field(DEFAULT_RATE_LIMIT_BURST, description="Burst capacity")

    # Storage
    database_url: str = Field(DEFAULT_DATABASE_URL, description="Database connection URL")
    session_timeout_hours: int = Field(DEFAULT_SESSION_TIMEOUT_HOURS, description="Session timeout")
    session_timeout_minutes: int = Field(
        default=120,
        description="Session timeout in minutes",
        ge=10,
        le=1440,  # Max 24 hours
    )
    max_sessions_per_user: int = Field(DEFAULT_MAX_SESSIONS_PER_USER, description="Max concurrent sessions")

    # Features
    enable_mcp: bool = Field(False, description="Enable Model Context Protocol")
    mcp_config_path: Path | None = Field(None, description="MCP configuration file path")
    enable_git_integration: bool = Field(True, description="Enable git commands")
    enable_file_uploads: bool = Field(True, description="Enable file upload handling")
    enable_quick_actions: bool = Field(True, description="Enable quick action buttons")
    agentic_mode: bool = Field(
        True,
        description="Conversational agentic mode (default) vs classic command mode",
    )

    # Output verbosity (0=quiet, 1=normal, 2=detailed)
    verbose_level: int = Field(
        1,
        description=(
            "Bot output verbosity: 0=quiet (final response only), "
            "1=normal (tool names + reasoning), "
            "2=detailed (tool inputs + longer reasoning)"
        ),
        ge=0,
        le=2,
    )

    # Monitoring
    log_level: str = Field("INFO", description="Logging level")
    enable_telemetry: bool = Field(False, description="Enable anonymous telemetry")
    sentry_dsn: str | None = Field(None, description="Sentry DSN for error tracking")

    # Development
    debug: bool = Field(False, description="Enable debug mode")
    development_mode: bool = Field(False, description="Enable development features")

    # Webhook settings (optional)
    webhook_url: str | None = Field(None, description="Webhook URL for bot")
    webhook_port: int = Field(8443, description="Webhook port")
    webhook_path: str = Field("/webhook", description="Webhook path")

    # Agentic platform settings
    enable_api_server: bool = Field(False, description="Enable FastAPI webhook server")
    api_server_port: int = Field(8080, description="Webhook API server port")
    enable_scheduler: bool = Field(False, description="Enable job scheduler")
    github_webhook_secret: str | None = Field(None, description="GitHub webhook HMAC secret")
    webhook_api_secret: str | None = Field(None, description="Shared secret for generic webhook providers")
    notification_chat_ids: list[int] | None = Field(
        None, description="Default Telegram chat IDs for proactive notifications"
    )
    enable_project_threads: bool = Field(
        False,
        description="Enable strict routing by Telegram forum project threads",
    )
    project_threads_mode: Literal["private", "group"] = Field(
        "private",
        description="Project thread mode: private chat topics or group forum topics",
    )
    project_threads_chat_id: int | None = Field(
        None, description="Telegram forum chat ID where project topics are managed"
    )
    projects_config_path: Path | None = Field(None, description="Path to YAML project registry for thread mode")

    # User profile and personalization
    user_profile_path: Path | None = Field(None, description="Path to user profile.md for context injection")
    user_name: str | None = Field(None, description="User name for personalization")
    user_timezone: str = Field("UTC", description="User timezone for scheduling and context")
    preferred_language: str = Field(
        "auto",
        description=(
            "Language for bot responses. 'auto' = detect from user messages. "
            "Use any language name or code (e.g. 'English', 'Romanian', 'ro', 'fr')."
        ),
    )

    # Voice transcription
    voice_provider: str | None = Field(None, description="Voice provider: 'groq' or 'local'")
    groq_api_key: SecretStr | None = Field(None, description="Groq API key for voice transcription")
    whisper_binary: str = Field("whisper-cpp", description="Path to whisper.cpp binary (local provider)")
    whisper_model_path: str | None = Field(None, description="Path to whisper.cpp model file")

    # Semantic memory
    enable_memory: bool = Field(False, description="Enable persistent semantic memory")
    enable_memory_embeddings: bool = Field(
        True, description="Use local embeddings for semantic search (requires sentence-transformers)"
    )
    memory_max_facts: int = Field(50, description="Max facts stored per user")
    memory_max_context_items: int = Field(10, description="Max memory items injected into context")

    # Proactive check-ins
    enable_checkins: bool = Field(False, description="Enable proactive check-ins via Claude")
    checkin_interval_minutes: int = Field(60, description="Check-in evaluation interval in minutes", ge=1)
    checkin_max_per_day: int = Field(2, description="Max proactive check-ins per day", ge=0)
    checkin_quiet_hours_start: int = Field(22, description="Quiet hours start (24h UTC)", ge=0, le=23)
    checkin_quiet_hours_end: int = Field(8, description="Quiet hours end (24h UTC)", ge=0, le=23)

    # Onboarding
    setup_completed: bool = Field(False, description="Telegram setup wizard completed")

    model_config = SettingsConfigDict(
        # Search consolidated home dir first, then fall back to project-root .env
        env_file=[
            str(Path.home() / ".claude-code-telegram" / "config" / ".env"),
            ".env",
        ],
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("allowed_users", "notification_chat_ids", mode="before")
    @classmethod
    def parse_int_list(cls, v: Any) -> list[int] | None:
        """Parse comma-separated integer lists."""
        if v is None:
            return None
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            return [int(uid.strip()) for uid in v.split(",") if uid.strip()]
        if isinstance(v, list):
            return [int(uid) for uid in v]
        return v

    @field_validator("claude_allowed_tools", mode="before")
    @classmethod
    def parse_claude_allowed_tools(cls, v: Any) -> list[str] | None:
        """Parse comma-separated tool names."""
        if v is None:
            return None
        if isinstance(v, str):
            return [tool.strip() for tool in v.split(",") if tool.strip()]
        if isinstance(v, list):
            return [str(tool) for tool in v]
        return v

    @field_validator("approved_directory", mode="before")
    @classmethod
    def validate_approved_directory(cls, v: Any) -> Path:
        """Ensure approved directory exists, creating it if needed."""
        if isinstance(v, str):
            if not v.strip():
                v = Path.home() / ".claude-code-telegram"
            else:
                v = Path(v)
        path = Path(v).expanduser().resolve()
        if path.exists() and not path.is_dir():
            raise ValueError(f"Approved directory is not a directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @field_validator("allowed_paths", mode="before")
    @classmethod
    def validate_allowed_paths(cls, v: Any) -> list[Path]:
        """Expand and resolve each allowed path, validating existence."""
        if not v:
            return []
        if isinstance(v, str):
            parts = [p.strip() for p in v.split(",") if p.strip()]
            v = parts
        result: list[Path] = []
        for p in v:
            path = Path(p).expanduser().resolve()
            if not path.exists():
                raise ValueError(f"Allowed path does not exist: {path}")
            if not path.is_dir():
                raise ValueError(f"Allowed path is not a directory: {path}")
            result.append(path)
        return result

    @field_validator("user_profile_path", mode="before")
    @classmethod
    def validate_user_profile_path(cls, v: Any) -> Path | None:
        """Expand ~ in user profile path."""
        if not v:
            return None
        if isinstance(v, str):
            if not v.strip():
                return None
            v = Path(v)
        return Path(v).expanduser().resolve()

    @field_validator("mcp_config_path", mode="before")
    @classmethod
    def validate_mcp_config(cls, v: Any, info: Any) -> Path | None:
        """Validate MCP configuration path if MCP is enabled."""
        if not v:
            return v
        if isinstance(v, str):
            v = Path(v)
        if not v.exists():
            raise ValueError(f"MCP config file does not exist: {v}")
        # Validate that the file contains valid JSON with mcpServers
        try:
            with open(v) as f:
                config_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP config file is not valid JSON: {e}")
        if not isinstance(config_data, dict):
            raise ValueError("MCP config file must contain a JSON object")
        if "mcpServers" not in config_data:
            raise ValueError(
                'MCP config file must contain a \'mcpServers\' key. Format: {"mcpServers": {"name": {"command": ...}}}'
            )
        if not isinstance(config_data["mcpServers"], dict):
            raise ValueError("'mcpServers' must be an object mapping server names to configurations")
        if not config_data["mcpServers"]:
            raise ValueError("'mcpServers' must contain at least one server configuration")
        return v

    @field_validator("projects_config_path", mode="before")
    @classmethod
    def validate_projects_config_path(cls, v: Any) -> Path | None:
        """Validate projects config path if provided."""
        if not v:
            return None
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return None
            v = Path(value)
        if not v.exists():
            raise ValueError(f"Projects config file does not exist: {v}")
        if not v.is_file():
            raise ValueError(f"Projects config path is not a file: {v}")
        return v

    @field_validator("project_threads_mode", mode="before")
    @classmethod
    def validate_project_threads_mode(cls, v: Any) -> str:
        """Validate project thread mode."""
        if v is None:
            return "private"
        mode = str(v).strip().lower()
        if mode not in {"private", "group"}:
            raise ValueError("project_threads_mode must be one of ['private', 'group']")
        return mode

    @field_validator("project_threads_chat_id", mode="before")
    @classmethod
    def validate_project_threads_chat_id(cls, v: Any) -> int | None:
        """Allow empty chat ID for private mode by treating blank values as None."""
        if v is None:
            return None
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return None
            return int(value)
        if isinstance(v, int):
            return v
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: Any) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}")
        return v.upper()

    @model_validator(mode="after")
    def validate_cross_field_dependencies(self) -> "Settings":
        """Validate dependencies between fields."""
        # Check auth token requirements
        if self.enable_token_auth and not self.auth_token_secret:
            raise ValueError("auth_token_secret required when enable_token_auth is True")

        # Check MCP requirements — auto-discover default if not set
        if self.enable_mcp and not self.mcp_config_path:
            default_mcp = Path.home() / ".claude-code-telegram" / "config" / "mcp.json"
            if default_mcp.exists():
                self.mcp_config_path = default_mcp
            else:
                raise ValueError("mcp_config_path required when enable_mcp is True")

        if self.enable_project_threads:
            if self.project_threads_mode == "group" and self.project_threads_chat_id is None:
                raise ValueError("project_threads_chat_id required when project_threads_mode is 'group'")
            if not self.projects_config_path:
                raise ValueError("projects_config_path required when enable_project_threads is True")

        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Priority: init kwargs > env vars > settings.toml > .env (legacy)."""
        from .toml_source import TomlSettingsSource

        return (
            init_settings,  # direct kwargs (tests, programmatic overrides)
            env_settings,  # system env vars always win over files
            TomlSettingsSource(settings_cls),  # settings.toml (primary config)
            dotenv_settings,  # .env legacy fallback
        )

    @property
    def all_allowed_paths(self) -> list[Path]:
        """Merged list of approved_directory + allowed_paths (deduplicated)."""
        seen: set[Path] = set()
        result: list[Path] = []
        for p in [self.approved_directory, *self.allowed_paths]:
            if p not in seen:
                seen.add(p)
                result.append(p)
        return result

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return not (self.debug or self.development_mode)

    @property
    def database_path(self) -> Path | None:
        """Extract path from SQLite database URL."""
        if self.database_url.startswith("sqlite:///"):
            db_path = self.database_url.replace("sqlite:///", "")
            return Path(db_path).resolve()
        return None

    @property
    def telegram_token_str(self) -> str:
        """Get Telegram token as string."""
        return self.telegram_bot_token.get_secret_value()

    @property
    def auth_secret_str(self) -> str | None:
        """Get auth token secret as string."""
        if self.auth_token_secret:
            return self.auth_token_secret.get_secret_value()
        return None

    @property
    def anthropic_api_key_str(self) -> str | None:
        """Get Anthropic API key as string."""
        return self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else None
