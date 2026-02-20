"""Main entry point for Claude Code Telegram Bot."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

import structlog

from src import __version__
from src.bot.core import ClaudeCodeBot
from src.bot.features.voice_handler import VoiceHandler
from src.claude import (
    ClaudeIntegration,
    SessionManager,
    ToolMonitor,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.features import FeatureFlags
from src.config.profile import ProfileManager
from src.config.settings import Settings
from src.config.soul import SoulManager
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.middleware import EventSecurityMiddleware
from src.exceptions import ConfigurationError
from src.memory.file_manager import MemoryFileManager
from src.memory.manager import MemoryManager
from src.notifications.service import NotificationService
from src.projects import ProjectThreadManager, load_project_registry
from src.scheduler.scheduler import JobScheduler
from src.security.audit import AuditLogger, SQLiteAuditStorage
from src.security.auth import (
    AuthenticationManager,
    SQLiteTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage
from src.utils.constants import APP_HOME

_PROFILE_TEMPLATE = """\
# User Profile

This file is injected into Claude's context at the start of every session.
Edit it to tell Claude about yourself, your preferences, and your workflow.

## About Me

- Name:
- Timezone: (e.g. Europe/Bucharest)
- Role/Occupation:

## Goals

-

## Work Context

- Current projects:
- Tools you use:
- Preferred programming languages:

## Communication Style

- Response length: concise / detailed
- Tone: casual / professional
- Include code examples: yes / no
"""

_SOUL_TEMPLATE = """\
# Soul

This file defines the assistant's identity and is injected into Claude's system_prompt.
Edit it to give Claude a consistent persona across all sessions.

## Identity

You are a capable, concise, and reliable assistant operating through Telegram.
You help with software engineering, DevOps, and general technical tasks.

## Principles

- Be direct and technical. Skip unnecessary preamble.
- Prefer minimal, working solutions over elaborate ones.
- When unsure, ask one targeted question rather than guessing.
- Keep responses focused — the user is on mobile.
"""

_MEMORY_TEMPLATE = """\
# Long-Term Memory

This file is your curated long-term memory. It is injected into your context on every session.
Use [MEMFILE: fact] tags in your responses to append new entries automatically.

## Facts

"""

_MCP_TEMPLATE = """\
{
  "mcpServers": {
    "_example_filesystem": {
      "_comment": "Remove _example_ prefix and adjust path to enable",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/directory"]
    },
    "_example_github": {
      "_comment": "Remove _example_ prefix and set GITHUB_PERSONAL_ACCESS_TOKEN env var",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": ""
      }
    }
  }
}
"""


def _bootstrap_optional_configs(app_home: Path) -> None:
    """Create profile.md, soul.md, memory.md, notes/, and mcp.json if absent."""
    logger = structlog.get_logger()

    profile_path = app_home / "config" / "profile.md"
    if not profile_path.exists():
        profile_path.write_text(_PROFILE_TEMPLATE, encoding="utf-8")
        logger.info("Created default profile.md", path=str(profile_path))

    soul_path = app_home / "config" / "soul.md"
    if not soul_path.exists():
        soul_path.write_text(_SOUL_TEMPLATE, encoding="utf-8")
        logger.info("Created default soul.md", path=str(soul_path))

    memory_path = app_home / "config" / "memory.md"
    if not memory_path.exists():
        memory_path.write_text(_MEMORY_TEMPLATE, encoding="utf-8")
        logger.info("Created default memory.md", path=str(memory_path))

    notes_dir = app_home / "config" / "notes"
    if not notes_dir.exists():
        notes_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Created notes directory", path=str(notes_dir))

    mcp_path = app_home / "config" / "mcp.json"
    if not mcp_path.exists():
        mcp_path.write_text(_MCP_TEMPLATE, encoding="utf-8")
        logger.info("Created default mcp.json template", path=str(mcp_path))


def bootstrap_dirs() -> None:
    """Create ~/.claude-code-telegram/ directory layout and migrate legacy files."""
    import shutil

    dirs = [
        APP_HOME / "config",
        APP_HOME / "data",
        APP_HOME / "logs",
        APP_HOME / "backups",
        APP_HOME / "backups" / "failed",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    logger = structlog.get_logger()

    # Migrate project-root .env → ~/.claude-code-telegram/config/.env (once)
    new_env = APP_HOME / "config" / ".env"
    old_env = Path(".env")
    if not new_env.exists() and old_env.exists():
        shutil.copy2(old_env, new_env)
        logger.info("Migrated .env to consolidated location", dst=str(new_env))

    # Migrate data/bot.db → ~/.claude-code-telegram/data/bot.db (once)
    new_db = APP_HOME / "data" / "bot.db"
    old_db = Path("data") / "bot.db"
    if not new_db.exists() and old_db.exists():
        shutil.copy2(old_db, new_db)
        logger.info("Migrated bot.db to consolidated location", dst=str(new_db))

    # Phase 2: migrate .env → settings.toml (or generate empty template)
    from src.config.toml_template import ensure_toml_config, migrate_env_to_toml

    if not migrate_env_to_toml():
        ensure_toml_config()

    # Bootstrap optional config files (profile.md, mcp.json)
    _bootstrap_optional_configs(APP_HOME)


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    level = logging.DEBUG if debug else logging.INFO

    # Configure standard logging
    logging.basicConfig(
        level=level,
        format="%(message)s",
        stream=sys.stdout,
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (structlog.processors.JSONRenderer() if not debug else structlog.dev.ConsoleRenderer()),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Code Telegram Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--version", action="version", version=f"Claude Code Telegram Bot {__version__}")

    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    parser.add_argument(
        "--no-wizard",
        action="store_true",
        help="Skip the interactive setup wizard even if config is incomplete",
    )

    return parser.parse_args()


async def create_application(config: Settings) -> dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Initialize storage system
    storage = Storage(config.database_url)
    await storage.initialize()

    # Create security components
    providers = []

    # Add whitelist provider if users are configured
    if config.allowed_users:
        providers.append(WhitelistAuthProvider(config.allowed_users))

    # Add token provider if enabled
    if config.enable_token_auth:
        token_storage = SQLiteTokenStorage(storage.db_manager)
        auth_secret = config.auth_secret_str
        assert auth_secret is not None
        providers.append(TokenAuthProvider(auth_secret, token_storage))

    # Fall back to allowing all users in development mode
    if not providers and config.development_mode:
        logger.warning("No auth providers configured - creating development-only allow-all provider")
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError("No authentication providers configured")

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.all_allowed_paths,
        disable_security_patterns=config.disable_security_patterns,
    )
    rate_limiter = RateLimiter(config)

    # Create audit storage and logger (SQLite-backed for persistence across restarts)
    audit_storage = SQLiteAuditStorage(storage.db_manager)
    audit_logger = AuditLogger(audit_storage)

    # Create Claude integration components with persistent storage
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)
    tool_monitor = ToolMonitor(config, security_validator, agentic_mode=config.agentic_mode)

    # Create Claude SDK manager
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(config)

    # Profile manager — use configured path or fall back to the default profile.md
    _profile_path = config.user_profile_path
    if not _profile_path:
        _default_profile = APP_HOME / "config" / "profile.md"
        if _default_profile.exists():
            _profile_path = _default_profile
    profile_manager = ProfileManager(_profile_path) if _profile_path else None

    # Soul manager — use configured path or fall back to the default soul.md
    _soul_path = config.soul_path
    if not _soul_path:
        _default_soul = APP_HOME / "config" / "soul.md"
        if _default_soul.exists():
            _soul_path = _default_soul
    soul_manager = SoulManager(_soul_path) if _soul_path else None

    # Memory file manager — curated long-term memory.md + notes/ directory
    _memory_file_path = config.memory_file_path
    if not _memory_file_path:
        _default_memory = APP_HOME / "config" / "memory.md"
        if _default_memory.exists():
            _memory_file_path = _default_memory
    _notes_dir = config.notes_dir
    if not _notes_dir:
        _default_notes = APP_HOME / "config" / "notes"
        if _default_notes.exists():
            _notes_dir = _default_notes
    memory_file_manager = MemoryFileManager(_memory_file_path, _notes_dir) if _memory_file_path else None

    # Memory manager (optional — only active when ENABLE_MEMORY=true)
    memory_manager: MemoryManager | None = None
    if config.enable_memory:
        memory_manager = MemoryManager(
            db_manager=storage.db_manager,
            enable_embeddings=config.enable_memory_embeddings,
        )
        logger.info("Semantic memory enabled", embeddings=config.enable_memory_embeddings)

    # Voice handler (optional — only active when VOICE_PROVIDER is set)
    voice_handler = VoiceHandler(config) if config.voice_provider else None

    # Create main Claude integration facade
    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
        tool_monitor=tool_monitor,
        profile_manager=profile_manager,
        memory_manager=memory_manager,
        soul_manager=soul_manager,
        memory_file_manager=memory_file_manager,
    )

    # --- Event bus and agentic platform components ---
    event_bus = EventBus()

    # Event security middleware
    event_security = EventSecurityMiddleware(
        event_bus=event_bus,
        security_validator=security_validator,
        auth_manager=auth_manager,
    )
    event_security.register()

    # Agent handler — translates events into Claude executions
    agent_handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=config.approved_directory,
        default_user_id=config.allowed_users[0] if config.allowed_users else 0,
    )
    agent_handler.register()

    # Create bot with all dependencies
    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "claude_integration": claude_integration,
        "storage": storage,
        "event_bus": event_bus,
        "project_registry": None,
        "project_threads_manager": None,
        "memory_manager": memory_manager,
        "voice_handler": voice_handler,
    }

    bot = ClaudeCodeBot(config, dependencies)

    # Notification service and scheduler need the bot's Telegram Bot instance,
    # which is only available after bot.initialize(). We store placeholders
    # and wire them up in run_application() after initialization.

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        "claude_integration": claude_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "event_bus": event_bus,
        "agent_handler": agent_handler,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
    }


async def run_application(app: dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: ClaudeCodeBot = app["bot"]
    claude_integration: ClaudeIntegration = app["claude_integration"]
    storage: Storage = app["storage"]
    config: Settings = app["config"]
    features: FeatureFlags = app["features"]
    event_bus: EventBus = app["event_bus"]

    notification_service: NotificationService | None = None
    scheduler: JobScheduler | None = None
    project_threads_manager: ProjectThreadManager | None = None

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        if signum == signal.SIGTERM:
            logger.info("Shutting down due to: SIGTERM received (restart or config change)")
        else:
            logger.info("Shutting down: user requested (SIGINT)")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Claude Code Telegram Bot")

        # Initialize the bot first (creates the Telegram Application)
        await bot.initialize()

        if config.enable_project_threads:
            if not config.projects_config_path:
                raise ConfigurationError("Project thread mode enabled but required settings are missing")
            registry = load_project_registry(
                config_path=config.projects_config_path,
                approved_directory=config.approved_directory,
            )
            project_threads_manager = ProjectThreadManager(
                registry=registry,
                repository=storage.project_threads,
            )

            bot.deps["project_registry"] = registry
            bot.deps["project_threads_manager"] = project_threads_manager

            if config.project_threads_mode == "group":
                if config.project_threads_chat_id is None:
                    raise ConfigurationError("Group thread mode requires PROJECT_THREADS_CHAT_ID")
                sync_result = await project_threads_manager.sync_topics(
                    bot.app.bot,
                    chat_id=config.project_threads_chat_id,
                )
                logger.info(
                    "Project thread startup sync complete",
                    mode=config.project_threads_mode,
                    chat_id=config.project_threads_chat_id,
                    created=sync_result.created,
                    reused=sync_result.reused,
                    renamed=sync_result.renamed,
                    failed=sync_result.failed,
                    deactivated=sync_result.deactivated,
                )

        # Now wire up components that need the Telegram Bot instance
        telegram_bot = bot.app.bot

        # Start event bus
        await event_bus.start()

        # Notification service
        notification_service = NotificationService(
            event_bus=event_bus,
            bot=telegram_bot,
            default_chat_ids=config.notification_chat_ids or [],
        )
        notification_service.register()
        await notification_service.start()

        # Collect concurrent tasks
        tasks = []

        # Bot task — use start() which handles its own initialization check
        bot_task = asyncio.create_task(bot.start())
        tasks.append(bot_task)

        # API server (if enabled)
        if features.api_server_enabled:
            from src.api.server import run_api_server

            api_task = asyncio.create_task(run_api_server(event_bus, config, storage.db_manager))
            tasks.append(api_task)
            logger.info("API server enabled", port=config.api_server_port)

        # Scheduler (if enabled)
        if features.scheduler_enabled:
            scheduler = JobScheduler(
                event_bus=event_bus,
                db_manager=storage.db_manager,
                default_working_directory=config.approved_directory,
            )
            await scheduler.start()
            logger.info("Job scheduler enabled")

            # Proactive check-ins (if enabled — require scheduler)
            if config.enable_checkins:
                from src.scheduler.checkin import CheckInService

                checkin_service = CheckInService(
                    claude_integration=claude_integration,
                    memory_manager=bot.deps.get("memory_manager"),
                    event_bus=event_bus,
                    db_manager=storage.db_manager,
                    settings=config,
                )
                await checkin_service.start(scheduler._scheduler)
                logger.info("Proactive check-ins enabled")

        # Shutdown task
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        # Wait for any task to complete or shutdown signal
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check completed tasks for exceptions
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        # Ordered shutdown: scheduler -> API -> notification -> bot -> claude -> storage
        logger.info("Shutting down application")

        try:
            if scheduler:
                await scheduler.stop()
            if notification_service:
                await notification_service.stop()
            await event_bus.stop()
            await bot.stop()
            await claude_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        logger.info("Application shutdown complete")


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = structlog.get_logger()
    logger.info("Starting Claude Code Telegram Bot", version=__version__)

    try:
        # Bootstrap directory structure and migrate legacy files before loading config
        bootstrap_dirs()

        # Run interactive setup wizard if needed (TTY only, skippable with --no-wizard)
        if not args.no_wizard and sys.stdin.isatty():
            from src.config.wizard import TOML_PATH, needs_wizard, run_wizard

            if needs_wizard(TOML_PATH):
                run_wizard(TOML_PATH)

        # Load configuration
        from src.config import FeatureFlags, load_config

        config = load_config(config_file=args.config_file)
        features = FeatureFlags(config)

        logger.info(
            "Configuration loaded",
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        # Initialize bot and Claude integration
        app = await create_application(config)
        await run_application(app)

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
        sys.exit(0)


if __name__ == "__main__":
    run()
