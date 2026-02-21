"""Main Telegram bot class.

Features:
- Command registration
- Handler management
- Context injection
- Graceful shutdown
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any, cast

import structlog
from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config.settings import Settings
from ..exceptions import ClaudeCodeTelegramError
from ..utils.constants import APP_HOME
from .features.registry import FeatureRegistry
from .orchestrator import MessageOrchestrator

logger = structlog.get_logger()

_RESTART_NOTIFY_FILE = APP_HOME / "data" / "restart_notify.json"


async def _send_restart_notification(app: Application) -> None:  # type: ignore[type-arg]
    """If a restart-notify file exists, send 'Bot restarted.' to the saved chat."""
    if not _RESTART_NOTIFY_FILE.exists():
        return
    try:
        data = json.loads(_RESTART_NOTIFY_FILE.read_text(encoding="utf-8"))
        chat_id: int = data["chat_id"]
        thread_id: int | None = data.get("message_thread_id")
        await app.bot.send_message(
            chat_id=chat_id,
            text="Bot restarted.",
            message_thread_id=thread_id,
        )
        logger.info("Sent restart notification", chat_id=chat_id)
    except Exception:
        logger.exception("Failed to send restart notification")
    finally:
        _RESTART_NOTIFY_FILE.unlink(missing_ok=True)


class ClaudeCodeBot:
    """Main bot orchestrator."""

    def __init__(self, settings: Settings, dependencies: dict[str, Any]):
        """Initialize bot with settings and dependencies."""
        self.settings = settings
        self.deps = dependencies
        self.app: Application | None = None
        self.is_running = False
        self.feature_registry: FeatureRegistry | None = None
        self.orchestrator = MessageOrchestrator(settings, dependencies)

    async def initialize(self) -> None:
        """Initialize bot application. Idempotent — safe to call multiple times."""
        if self.app is not None:
            return

        logger.info("Initializing Telegram bot")

        # Create application
        builder = Application.builder()
        builder.token(self.settings.telegram_token_str)

        # Configure connection settings
        builder.connect_timeout(30)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.pool_timeout(30)

        self.app = builder.build()
        assert self.app is not None

        # Initialize feature registry
        from ..security.validators import SecurityValidator
        from ..storage.facade import Storage

        storage = cast(Storage, self.deps["storage"])
        security = cast(SecurityValidator, self.deps["security_validator"])
        self.feature_registry = FeatureRegistry(
            config=self.settings,
            storage=storage,
            security=security,
        )

        # Add feature registry to dependencies
        self.deps["features"] = self.feature_registry

        # Set bot commands for menu
        await self._set_bot_commands()

        # Register handlers
        self._register_handlers()

        # Add middleware
        self._add_middleware()

        # Set error handler
        self.app.add_error_handler(self._error_handler)  # type: ignore[arg-type]

        logger.info("Bot initialization complete")

    async def _set_bot_commands(self) -> None:
        """Set bot command menu via orchestrator."""
        assert self.app is not None
        commands = await self.orchestrator.get_bot_commands()
        await self.app.bot.set_my_commands(commands)
        logger.info("Bot commands set", commands=[cmd.command for cmd in commands])

    def _register_handlers(self) -> None:
        """Register handlers via orchestrator (mode-aware)."""
        assert self.app is not None
        self.orchestrator.register_handlers(self.app)

    def _add_middleware(self) -> None:
        """Add middleware to application."""
        assert self.app is not None
        from .middleware.auth import auth_middleware
        from .middleware.rate_limit import rate_limit_middleware
        from .middleware.security import security_middleware

        # Middleware runs in order of group numbers (lower = earlier)
        # Security middleware first (validate inputs)
        self.app.add_handler(
            MessageHandler(filters.ALL, self._create_middleware_handler(security_middleware)),
            group=-3,
        )

        # Authentication second
        self.app.add_handler(
            MessageHandler(filters.ALL, self._create_middleware_handler(auth_middleware)),
            group=-2,
        )

        # Rate limiting third
        self.app.add_handler(
            MessageHandler(filters.ALL, self._create_middleware_handler(rate_limit_middleware)),
            group=-1,
        )

        logger.info("Middleware added to bot")

    def _create_middleware_handler(self, middleware_func: Callable) -> Callable:
        """Create middleware handler that injects dependencies.

        When middleware rejects a request (returns without calling the handler),
        ApplicationHandlerStop is raised to prevent subsequent handler groups
        from processing the update.
        """
        from telegram.ext import ApplicationHandlerStop

        async def middleware_wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            # Ignore updates generated by bots (including this bot) to avoid
            # self-authentication loops and duplicate processing.
            if update.effective_user and getattr(update.effective_user, "is_bot", False):
                logger.debug(
                    "Skipping bot-originated update in middleware",
                    user_id=update.effective_user.id,
                    middleware=getattr(middleware_func, "__name__", repr(middleware_func)),
                )
                raise ApplicationHandlerStop

            # Inject dependencies into context
            bot_data = cast(dict[str, Any], context.bot_data)
            for key, value in self.deps.items():
                bot_data[key] = value
            bot_data["settings"] = self.settings

            # Track whether the middleware allowed the request through
            handler_called = False

            async def dummy_handler(event: Any, data: Any) -> None:
                nonlocal handler_called
                handler_called = True

            # Call middleware with Telegram-style parameters
            await middleware_func(dummy_handler, update, context.bot_data)

            # If middleware didn't call the handler, it rejected the request.
            # Raise ApplicationHandlerStop to prevent subsequent handler groups
            # (including the main message handlers) from processing this update.
            if not handler_called:
                raise ApplicationHandlerStop()

        return middleware_wrapper

    async def start(self) -> None:
        """Start the bot."""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        await self.initialize()
        assert self.app is not None

        logger.info("Starting bot", mode="webhook" if self.settings.webhook_url else "polling")

        try:
            self.is_running = True

            if self.settings.webhook_url:
                # Webhook mode
                await self.app.run_webhook(  # type: ignore[misc]
                    listen="0.0.0.0",
                    port=self.settings.webhook_port,
                    url_path=self.settings.webhook_path,
                    webhook_url=self.settings.webhook_url,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
            else:
                # Polling mode - initialize and start polling manually
                await self.app.initialize()
                await self.app.start()
                updater = self.app.updater
                assert updater is not None
                await updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )

                await _send_restart_notification(self.app)

                # Keep running until manually stopped
                while self.is_running:
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error("Error running bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to start bot: {str(e)}") from e
        finally:
            self.is_running = False

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if not self.is_running:
            logger.warning("Bot is not running")
            return

        logger.info("Stopping bot")

        try:
            self.is_running = False  # Stop the main loop first

            # Shutdown feature registry
            if self.feature_registry:
                self.feature_registry.shutdown()

            if self.app:
                # Stop the updater if it's running
                updater = self.app.updater
                if updater and updater.running:
                    await updater.stop()

                # Stop the application
                await self.app.stop()
                await self.app.shutdown()

            logger.info("Bot stopped successfully")
        except Exception as e:
            logger.error("Error stopping bot", error=str(e))
            raise ClaudeCodeTelegramError(f"Failed to stop bot: {str(e)}") from e

    async def _error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle errors globally."""
        import telegram.error as tg_error

        error = context.error

        # Transient network errors are expected (DNS blips, timeouts during polling).
        # Log at debug level and skip audit — they self-recover and are not actionable.
        if isinstance(error, (tg_error.NetworkError, tg_error.TimedOut)):
            logger.debug(
                "Transient network error during polling (self-healing)",
                error=str(error),
                error_type=type(error).__name__,
            )
            return

        logger.error(
            "Global error handler triggered",
            error=str(error),
            update_type=type(update).__name__ if update else None,
            user_id=(update.effective_user.id if update and update.effective_user else None),
        )

        # Determine error message for user
        from ..exceptions import (
            AuthenticationError,
            ConfigurationError,
            RateLimitExceeded,
            SecurityError,
        )

        error_messages = {
            AuthenticationError: "🔒 Authentication required. Please contact the administrator.",
            SecurityError: "🛡️ Security violation detected. This incident has been logged.",
            RateLimitExceeded: "⏱️ Rate limit exceeded. Please wait before sending more messages.",
            ConfigurationError: "⚙️ Configuration error. Please contact the administrator.",
            asyncio.TimeoutError: "⏰ Operation timed out. Please try again with a simpler request.",
        }

        error_type = type(error)
        user_message = error_messages.get(error_type, "❌ An unexpected error occurred. Please try again.")

        # Try to notify user
        if update and update.effective_message:
            try:
                await update.effective_message.reply_text(user_message)
            except Exception:
                logger.exception("Failed to send error message to user")

        # Log to audit system if available
        from ..security.audit import AuditLogger

        audit_logger: AuditLogger | None = cast(dict[str, Any], context.bot_data).get("audit_logger")
        if audit_logger and update and update.effective_user:
            try:
                await audit_logger.log_security_violation(
                    user_id=update.effective_user.id,
                    violation_type="system_error",
                    details=f"Error type: {error_type.__name__}, Message: {str(error)}",
                    severity="medium",
                )
            except Exception:
                logger.exception("Failed to log error to audit system")

    async def get_bot_info(self) -> dict[str, Any]:
        """Get bot information."""
        if not self.app:
            return {"status": "not_initialized"}

        try:
            me = await self.app.bot.get_me()
            return {
                "status": "running" if self.is_running else "initialized",
                "username": me.username,
                "first_name": me.first_name,
                "id": me.id,
                "can_join_groups": me.can_join_groups,
                "can_read_all_group_messages": me.can_read_all_group_messages,
                "supports_inline_queries": me.supports_inline_queries,
                "webhook_url": self.settings.webhook_url,
                "webhook_port": (self.settings.webhook_port if self.settings.webhook_url else None),
            }
        except Exception as e:
            logger.error("Failed to get bot info", error=str(e))
            return {"status": "error", "error": str(e)}

    async def health_check(self) -> bool:
        """Perform health check."""
        try:
            if not self.app:
                return False

            # Try to get bot info
            await self.app.bot.get_me()
            return True
        except Exception as e:
            logger.error("Health check failed", error=str(e))
            return False
