"""Generate settings.toml template and migrate existing .env files.

On first startup after Phase 2 upgrade:
  - If ~/.claude-code-telegram/config/.env exists and settings.toml does not:
    auto-migrate by converting .env values to TOML native types and writing
    settings.toml. The .env is renamed to .env.migrated.
  - If neither exists: write the commented default template so the user has a
    ready-to-edit file.

The template is generated programmatically (not a static file) so it always
reflects current field defaults.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args, get_origin

import structlog
import tomlkit
from dotenv import dotenv_values

from src.utils.constants import APP_HOME

logger = structlog.get_logger()

TOML_PATH = APP_HOME / "config" / "settings.toml"
ENV_PATH = APP_HOME / "config" / ".env"
MIGRATED_ENV_PATH = APP_HOME / "config" / ".env.migrated"

# ── Template text ─────────────────────────────────────────────────────────────

_TEMPLATE = """\
# Claude Code Telegram Bot — settings.toml
# https://github.com/talpah/claude-code-telegram
#
# Edit this file to configure the bot. Restart required for most changes.
# Settings marked [live] can be changed via /settings in Telegram and take
# effect immediately without a restart.
#
# Generated automatically on first run. Your edits and comments are preserved.

# ── Required ──────────────────────────────────────────────────────────────────

[required]
# Telegram bot token from @BotFather
telegram_bot_token = ""
# Bot username without @
telegram_bot_username = ""
# Absolute path to the workspace directory (the "sandbox")
approved_directory = ""
# Telegram user IDs allowed to use the bot. Example: [123456789, 987654321]
allowed_users = []

# ── Authentication ────────────────────────────────────────────────────────────

[auth]
enable_token_auth = false
# Required when enable_token_auth = true. Generate: openssl rand -hex 32
auth_token_secret = ""

# ── Claude ────────────────────────────────────────────────────────────────────

[claude]
use_sdk = true
# Anthropic API key (optional if Claude CLI is logged in)
anthropic_api_key = ""
# Path to Claude CLI executable (leave empty to use $PATH)
claude_cli_path = ""
# Model to use. Common values: claude-sonnet-4-6, claude-opus-4-6, claude-haiku-4-5
# [live] changeable via /settings or /model without restart
claude_model = "claude-sonnet-4-5"
# Maximum conversation turns before forcing a new session. [live]
claude_max_turns = 10
# Response timeout in seconds. [live]
claude_timeout_seconds = 300
# Maximum Claude API cost per user in USD
claude_max_cost_per_user = 10.0
# Allowed Claude tools (empty list = use defaults)
claude_allowed_tools = [
    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
    "LS", "Task", "MultiEdit", "NotebookRead", "NotebookEdit",
    "WebFetch", "TodoRead", "TodoWrite", "WebSearch",
]
# Explicitly disallowed tools (takes precedence over allowed list)
claude_disallowed_tools = []

# ── Sandbox ───────────────────────────────────────────────────────────────────

[sandbox]
# OS-level bash sandboxing for the approved directory. [live]
sandbox_enabled = true
# Commands exempt from sandboxing (need system access)
sandbox_excluded_commands = ["git", "npm", "pip", "poetry", "make", "docker"]

# ── Security ──────────────────────────────────────────────────────────────────

[security]
# Disable dangerous pattern validation (pipes, redirections, etc.)
# Only enable in fully trusted environments. [live]
disable_security_patterns = false
# Allow all Claude tools without validation. [live]
disable_tool_validation = false

# ── Rate Limiting ─────────────────────────────────────────────────────────────

[rate_limit]
# Maximum requests per window per user. [live]
rate_limit_requests = 10
# Rate limit window in seconds. [live]
rate_limit_window = 60
# Burst capacity (allows short spikes above rate_limit_requests)
rate_limit_burst = 20

# ── Storage ───────────────────────────────────────────────────────────────────

[storage]
# SQLite database URL (default uses ~/.claude-code-telegram/data/bot.db)
database_url = ""
session_timeout_hours = 24
session_timeout_minutes = 120
max_sessions_per_user = 5

# ── Features ──────────────────────────────────────────────────────────────────

[features]
# Conversational agentic mode (default). Set false for classic command mode.
agentic_mode = true
# Enable Model Context Protocol (requires mcp_config_path)
enable_mcp = false
# Path to MCP configuration JSON file
mcp_config_path = ""
enable_git_integration = true
enable_file_uploads = true
enable_quick_actions = true

# ── Output ────────────────────────────────────────────────────────────────────

[output]
# Verbosity: 0=quiet (final only), 1=normal (tool names), 2=detailed (inputs)
# [live] changeable via /settings or /verbose without restart
verbose_level = 1

# ── Monitoring ────────────────────────────────────────────────────────────────

[monitoring]
# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
log_level = "INFO"
enable_telemetry = false
# Sentry DSN for error tracking (leave empty to disable)
sentry_dsn = ""

# ── Development ───────────────────────────────────────────────────────────────

[development]
debug = false
development_mode = false

# ── Webhooks & API Server ─────────────────────────────────────────────────────

[webhook]
webhook_url = ""
webhook_port = 8443
webhook_path = "/webhook"
enable_api_server = false
api_server_port = 8080
enable_scheduler = false
# GitHub webhook HMAC secret for signature verification
github_webhook_secret = ""
# Shared secret for generic webhook providers (Bearer token)
webhook_api_secret = ""
# Default Telegram chat IDs for proactive notifications
notification_chat_ids = []

# ── Project Threads ───────────────────────────────────────────────────────────

[projects]
enable_project_threads = false
# "private" (DM topics) or "group" (forum topics)
project_threads_mode = "private"
project_threads_chat_id = 0
# Path to YAML project registry (required when enable_project_threads = true)
projects_config_path = ""

# ── Personalization ───────────────────────────────────────────────────────────

[personalization]
# Path to user profile markdown (injected into Claude's context)
user_profile_path = ""
user_name = ""
user_timezone = "UTC"

# ── Voice Transcription ───────────────────────────────────────────────────────

[voice]
# Provider: "groq" or "local" (leave empty to disable)
voice_provider = ""
groq_api_key = ""
whisper_binary = "whisper-cpp"
whisper_model_path = ""

# ── Memory ────────────────────────────────────────────────────────────────────

[memory]
# Persistent semantic memory across conversations. [live]
enable_memory = false
enable_memory_embeddings = true
# Maximum facts stored per user. [live]
memory_max_facts = 50
# Maximum memory items injected into context. [live]
memory_max_context_items = 10

# ── Check-ins ─────────────────────────────────────────────────────────────────

[checkins]
# Proactive check-ins via Claude (requires enable_scheduler = true). [live]
enable_checkins = false
# Evaluation interval in minutes. [live]
checkin_interval_minutes = 30
# Maximum proactive check-ins per day. [live]
checkin_max_per_day = 3
# Quiet hours start (24h UTC) — no check-ins sent during quiet hours
checkin_quiet_hours_start = 22
checkin_quiet_hours_end = 8
"""


# ── Public API ────────────────────────────────────────────────────────────────


def ensure_toml_config(toml_path: Path = TOML_PATH) -> None:
    """Write the commented default template if settings.toml is absent."""
    if toml_path.exists():
        return
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(_TEMPLATE, encoding="utf-8")
    logger.info("Created default settings.toml", path=str(toml_path))


def migrate_env_to_toml(
    env_path: Path = ENV_PATH,
    toml_path: Path = TOML_PATH,
    migrated_path: Path = MIGRATED_ENV_PATH,
) -> bool:
    """Migrate a .env file to settings.toml (once).

    Returns True if migration was performed, False if skipped.
    """
    if toml_path.exists() or not env_path.exists():
        return False

    env_values = dict(dotenv_values(env_path))
    doc = _build_document_from_env(env_values)

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    env_path.rename(migrated_path)

    logger.info(
        "Migrated .env to settings.toml",
        toml=str(toml_path),
        archived=str(migrated_path),
    )
    return True


# ── Internal helpers ──────────────────────────────────────────────────────────


def _build_document_from_env(env_values: dict[str, str]) -> tomlkit.TOMLDocument:
    """Build a tomlkit document from env-var values, using the template as a base."""
    # Lazy import to avoid circular dependency at module load
    from src.config.settings import Settings
    from src.config.toml_source import FIELD_TO_SECTION

    # Parse the template to get a document with all comments/structure
    doc = tomlkit.parse(_TEMPLATE)

    model_fields = Settings.model_fields

    for env_key, raw_value in env_values.items():
        field_name = env_key.lower()
        if field_name not in model_fields:
            continue
        section = FIELD_TO_SECTION.get(field_name)
        if section is None or section not in doc:
            continue

        coerced = _coerce(field_name, raw_value, model_fields)
        if coerced is None:
            continue

        doc[section][field_name] = coerced  # type: ignore[index]

    return doc


def _coerce(field_name: str, raw: str, model_fields: dict[str, Any]) -> Any:
    """Convert a raw .env string to the appropriate TOML native type.

    Returns None to signal "omit this key" (e.g. empty optional fields).
    """
    if not raw and raw != "0":
        return None  # empty string → omit

    field_info = model_fields.get(field_name)
    if field_info is None:
        return raw  # unknown field → pass through as string

    annotation = field_info.annotation
    origin = get_origin(annotation)

    # Union types (e.g. str | None, list[int] | None) — unwrap
    if origin is type(None):
        return None
    inner = _unwrap_union(annotation)

    # bool
    if inner is bool:
        return raw.lower() in ("true", "1", "yes")

    # int
    if inner is int:
        try:
            return int(raw)
        except ValueError:
            return None

    # float
    if inner is float:
        try:
            return float(raw)
        except ValueError:
            return None

    # list[int] (allowed_users, notification_chat_ids)
    if get_origin(inner) is list:
        item_args = get_args(inner)
        item_type = item_args[0] if item_args else str
        parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
        try:
            return [item_type(p) for p in parts]
        except (ValueError, TypeError):
            return parts  # fall back to list of strings

    # Path
    if inner is Path:
        return raw if raw else None

    # Everything else: string (SecretStr, Literal, plain str)
    return raw


def _unwrap_union(annotation: Any) -> Any:
    """Return the non-None type from Union[X, None] / X | None."""
    origin = get_origin(annotation)
    # Python 3.10+ union (X | Y) and typing.Union both use get_origin/get_args
    if origin is type(None):
        return type(None)
    args = get_args(annotation)
    if args:
        non_none = [a for a in args if a is not type(None)]
        return non_none[0] if non_none else annotation
    return annotation
