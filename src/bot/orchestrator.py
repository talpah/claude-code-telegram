"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import structlog
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.exceptions import ClaudeToolValidationError
from ..claude.integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.html_format import escape_html

logger = structlog.get_logger()


def _bd(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Cast context.bot_data to a concrete dict type."""
    return cast(dict[str, Any], context.bot_data)


def _ud(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Cast context.user_data to a concrete dict type."""
    return cast(dict[str, Any], context.user_data)


# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: dict[str, Any]):
        self.settings = settings
        self.deps = deps

    def _inject_deps(self, handler: Callable) -> Callable:
        """Wrap handler to inject dependencies into _bd(context)."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                _bd(context)[key] = value
            _bd(context)["settings"] = self.settings
            _ud(context).pop("_thread_context", None)

            is_sync_bypass = getattr(handler, "__name__", "") == "sync_threads"
            is_start_bypass = getattr(handler, "__name__", "") in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (is_start_bypass and message_thread_id is None)
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = _bd(context).get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\nThread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = _ud(context).setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = Path(current_dir_raw).resolve() if current_dir_raw else project_root
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        _ud(context)["current_directory"] = current_dir
        _ud(context)["claude_session_id"] = state.get("claude_session_id")
        _ud(context)["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = _ud(context).get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = _ud(context).get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = _ud(context).setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": _ud(context).get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> int | None:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")  # type: ignore[union-attr]
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("memory", self.agentic_memory),
            ("model", self.agentic_model),
            ("reload", self.agentic_reload),
            ("settings", self.agentic_settings),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(filters.Document.ALL, self._inject_deps(self.agentic_document)),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice / audio messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # cd: callbacks — switch directory / resume session
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        # set: callbacks — /settings interactive menu
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._settings_callback),
                pattern=r"^set:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.Document.ALL, self._inject_deps(message.handle_document)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(CallbackQueryHandler(self._inject_deps(callback.handle_callback_query)))

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("memory", "Show Claude's memory about you"),
                BotCommand("model", "Show or change Claude model"),
                BotCommand("reload", "Restart the bot process"),
                BotCommand("settings", "Interactive settings menu (owner only)"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Brief welcome, no buttons."""
        assert update.message is not None
        assert update.effective_user is not None
        user = update.effective_user
        sync_line = ""
        if self.settings.enable_project_threads and self.settings.project_threads_mode == "private":
            if not update.effective_chat or getattr(update.effective_chat, "type", "") != "private":
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = _bd(context).get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = f"\n\n🧵 Topics synced (created {result.created}, reused {result.reused})."
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = _ud(context).get("current_directory", self.settings.approved_directory)
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new · /status · /verbose · /repo · /memory · /model · /reload · /settings"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset session, one-line confirmation."""
        assert update.message is not None
        _ud(context)["claude_session_id"] = None
        _ud(context)["session_started"] = True
        _ud(context)["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Compact one-line status, no buttons."""
        assert update.message is not None
        assert update.effective_user is not None
        current_dir = _ud(context).get("current_directory", self.settings.approved_directory)
        dir_display = str(current_dir)

        session_id = _ud(context).get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = _bd(context).get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(f"📂 {dir_display} · Session: {session_status}{cost_str}")

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = _ud(context).get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        assert update.message is not None
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please use: /verbose 0, /verbose 1, or /verbose 2")
            return

        _ud(context)["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: list[dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: list[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: list[dict[str, Any]],
        start_time: float,
    ) -> Callable[[StreamUpdate], Any] | None:
        """Create a stream callback for verbose progress updates.

        Returns None when verbose_level is 0 (nothing to display).
        Typing indicators are handled by a separate heartbeat task.
        """
        if verbose_level == 0:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    tool_log.append({"kind": "tool", "name": name, "detail": detail})

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text and verbose_level >= 1:
                    # Collapse to first meaningful line, cap length
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        tool_log.append({"kind": "text", "detail": first_line[:120]})

            # Throttle progress message edits to avoid Telegram rate limits
            now = time.time()
            if (now - last_edit_time[0]) >= 2.0 and tool_log:
                last_edit_time[0] = now
                new_text = self._format_verbose_progress(tool_log, verbose_level, start_time)
                try:
                    await progress_msg.edit_text(new_text)
                except Exception:
                    pass

        return _on_stream

    async def agentic_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        assert update.message is not None
        assert update.effective_user is not None
        message_text = update.message.text or ""
        await self._run_agentic_prompt(update, context, message_text)

    async def agentic_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Transcribe voice/audio message and route through Claude."""
        assert update.message is not None
        assert update.effective_user is not None

        voice_handler = _bd(context).get("voice_handler")
        if not voice_handler:
            await update.message.reply_text(
                "Voice transcription not configured. Set VOICE_PROVIDER (groq or local) in settings."
            )
            return

        voice = update.message.voice or update.message.audio
        if not voice:
            return

        progress_msg = await update.message.reply_text("Transcribing voice...")
        try:
            file = await voice.get_file()
            ogg_bytes = bytes(await file.download_as_bytearray())
            transcribed = await voice_handler.transcribe(ogg_bytes)
        except Exception as e:
            logger.error("Voice transcription failed", error=str(e), user_id=update.effective_user.id)
            await progress_msg.edit_text(f"Voice transcription failed: {e}")
            return

        await progress_msg.delete()
        prompt = f"🎤 Voice: {transcribed}"
        await self._run_agentic_prompt(update, context, prompt)

    async def _run_agentic_prompt(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
    ) -> None:
        """Execute a prompt through Claude and deliver the response. Shared by text and voice handlers."""
        assert update.message is not None
        assert update.effective_user is not None
        user_id = update.effective_user.id

        logger.info("Agentic prompt", user_id=user_id, prompt_length=len(prompt))

        # Rate limit check
        rate_limiter = _bd(context).get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.0)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)
        progress_msg = await update.message.reply_text("Working...")

        claude_integration = _bd(context).get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text("Claude integration not available. Check configuration.")
            return

        current_dir = _ud(context).get("current_directory", self.settings.approved_directory)
        session_id = _ud(context).get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(_ud(context).get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: list[dict[str, Any]] = []
        start_time = time.time()
        on_stream = self._make_stream_callback(verbose_level, progress_msg, tool_log, start_time)

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                _ud(context)["force_new_session"] = False

            _ud(context)["claude_session_id"] = claude_response.session_id

            # Track actual cost post-execution
            if rate_limiter and claude_response.cost and claude_response.cost > 0:
                await rate_limiter.check_rate_limit(user_id, claude_response.cost, 0)

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(claude_response, context, self.settings, user_id)

            # Store interaction
            storage = _bd(context).get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=prompt,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Process memory tags from Claude's response
            memory_manager = _bd(context).get("memory_manager")
            if memory_manager and claude_response.content:
                try:
                    processed = await memory_manager.process_response(user_id, claude_response.content)
                    if processed:
                        logger.debug("Memory updated", count=len(processed))
                except Exception as e:
                    logger.warning("Memory processing failed", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(claude_response.content)

        except ClaudeToolValidationError as e:
            success = False
            logger.error("Tool validation error", error=str(e), user_id=user_id)
            from .utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage(str(e), parse_mode="HTML")]

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [FormattedMessage(_format_error_message(str(e)), parse_mode="HTML")]
        finally:
            heartbeat.cancel()

        await progress_msg.delete()

        for i, msg in enumerate(formatted_messages):
            try:
                await update.message.reply_text(
                    msg.text,
                    parse_mode=msg.parse_mode,
                    reply_markup=None,  # No keyboards in agentic mode
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send HTML response, retrying as plain text",
                    error=str(e),
                    message_index=i,
                )
                try:
                    await update.message.reply_text(
                        msg.text,
                        reply_markup=None,
                        reply_to_message_id=(update.message.message_id if i == 0 else None),
                    )
                except Exception:
                    await update.message.reply_text(
                        "Failed to send response. Please try again.",
                        reply_to_message_id=(update.message.message_id if i == 0 else None),
                    )

        # Audit log
        audit_logger = _bd(context).get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[prompt[:100]],
                success=success,
            )

    async def agentic_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process file upload -> Claude, minimal chrome."""
        assert update.message is not None
        assert update.effective_user is not None
        assert update.message.document is not None
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = _bd(context).get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        file_size = document.file_size or 0
        if file_size > max_size:
            await update.message.reply_text(f"File too large ({file_size / 1024 / 1024:.1f}MB). Max: 10MB.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = _bd(context).get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: str | None = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_name = document.file_name or "uploaded_file"
            caption = update.message.caption or "Please review this file:"

            if Path(file_name).suffix.lower() == ".pdf":
                # Save PDF to working dir so Claude's Read tool can access it
                current_dir = _ud(context).get("current_directory", self.settings.approved_directory)
                pdf_path = Path(current_dir) / file_name
                await file.download_to_drive(str(pdf_path))
                prompt = f"{caption}\n\nPlease read the file `{file_name}` and assist with the request."
            else:
                file_bytes = await file.download_as_bytearray()
                try:
                    content = file_bytes.decode("utf-8")
                    if len(content) > 50000:
                        content = content[:50000] + "\n... (truncated)"
                    prompt = f"{caption}\n\n**File:** `{file_name}`\n\n```\n{content}\n```"
                except UnicodeDecodeError:
                    await progress_msg.edit_text("Unsupported file format. Must be text-based (UTF-8).")
                    return

        # Process with Claude
        claude_integration = _bd(context).get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text("Claude integration not available. Check configuration.")
            return

        current_dir = _ud(context).get("current_directory", self.settings.approved_directory)
        session_id = _ud(context).get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(_ud(context).get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: list[dict[str, Any]] = []
        on_stream = self._make_stream_callback(verbose_level, progress_msg, tool_log, time.time())

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
            )

            if force_new:
                _ud(context)["force_new_session"] = False

            _ud(context)["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(claude_response, context, self.settings, user_id)

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(claude_response.content)

            await progress_msg.delete()

            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(str(e)), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Process photo -> Claude, minimal chrome."""
        assert update.message is not None
        assert update.effective_user is not None
        user_id = update.effective_user.id

        features = _bd(context).get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(photo, update.message.caption)

            claude_integration = _bd(context).get("claude_integration")
            if not claude_integration:
                await progress_msg.edit_text("Claude integration not available. Check configuration.")
                return

            current_dir = _ud(context).get("current_directory", self.settings.approved_directory)
            session_id = _ud(context).get("claude_session_id")

            # Check if /new was used — skip auto-resume for this first message.
            # Flag is only cleared after a successful run so retries keep the intent.
            force_new = bool(_ud(context).get("force_new_session"))

            verbose_level = self._get_verbose_level(context)
            tool_log: list[dict[str, Any]] = []
            on_stream = self._make_stream_callback(verbose_level, progress_msg, tool_log, time.time())

            heartbeat = self._start_typing_heartbeat(chat)
            try:
                claude_response = await claude_integration.run_command(
                    prompt=processed_image.prompt,
                    working_directory=current_dir,
                    user_id=user_id,
                    session_id=session_id,
                    on_stream=on_stream,
                    force_new=force_new,
                )
            finally:
                heartbeat.cancel()

            if force_new:
                _ud(context)["force_new_session"] = False

            _ud(context)["claude_session_id"] = claude_response.session_id

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(claude_response.content)

            await progress_msg.delete()

            for i, message in enumerate(formatted_messages):
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(str(e)), parse_mode="HTML")
            logger.error("Claude photo processing failed", error=str(e), user_id=user_id)

    async def agentic_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show Claude's persistent memory about the user (facts + active goals)."""
        assert update.message is not None
        assert update.effective_user is not None
        user_id = update.effective_user.id

        memory_manager = _bd(context).get("memory_manager")
        if not memory_manager:
            await update.message.reply_text(
                "Memory is not enabled.\n\nSet <code>ENABLE_MEMORY=true</code> in your <code>.env</code> to activate.",
                parse_mode="HTML",
            )
            return

        facts = await memory_manager.get_facts(user_id, limit=20)
        goals = await memory_manager.get_active_goals(user_id)

        if not facts and not goals:
            await update.message.reply_text(
                "No memories yet.\n\n"
                "Claude stores facts automatically when you share information worth remembering, "
                "and tracks goals you mention. Just chat normally — it happens in the background.",
                parse_mode="HTML",
            )
            return

        lines: list[str] = ["<b>Claude's memory about you</b>"]

        if facts:
            lines.append(f"\n<b>Facts</b> ({len(facts)})")
            for fact in facts:
                lines.append(f"• {escape_html(fact.content)}")

        if goals:
            lines.append(f"\n<b>Active Goals</b> ({len(goals)})")
            for goal in goals:
                deadline = f" <i>(deadline: {escape_html(goal.deadline)})</i>" if goal.deadline else ""
                lines.append(f"• {escape_html(goal.content)}{deadline}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # Aliases / short names → full model IDs
    _MODEL_ALIASES: dict[str, str] = {
        # Claude 4.6 family (latest)
        "opus": "claude-opus-4-6",
        "opus46": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "sonnet46": "claude-sonnet-4-6",
        # Claude 4.5 family
        "opus45": "claude-opus-4-5",
        "opusplan": "claude-opus-4-5",
        "sonnet45": "claude-sonnet-4-5",
        "haiku": "claude-haiku-4-5",
        "haiku4": "claude-haiku-4-5",
        "haiku45": "claude-haiku-4-5",
        # Claude 3 / 3.5 family
        "opus3": "claude-3-opus-20240229",
        "sonnet3": "claude-3-5-sonnet-20241022",
        "haiku3": "claude-3-5-haiku-20241022",
    }

    async def agentic_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/model [name] — show or change the active Claude model."""
        assert update.message is not None
        args = update.message.text.split()[1:] if update.message.text else []

        if not args:
            current = self.settings.claude_model
            alias_lines = "\n".join(
                f"  <code>{alias}</code> → <code>{model}</code>" for alias, model in sorted(self._MODEL_ALIASES.items())
            )
            await update.message.reply_text(
                f"Current model: <code>{escape_html(current)}</code>\n\n"
                f"Usage: <code>/model &lt;name&gt;</code>\n\n"
                f"Aliases:\n{alias_lines}\n\n"
                "Any full model ID (e.g. <code>claude-opus-4-6</code>) is also accepted.",
                parse_mode="HTML",
            )
            return

        raw = args[0].strip().lower()
        new_model = self._MODEL_ALIASES.get(raw, args[0].strip())

        old_model = self.settings.claude_model
        if new_model == old_model:
            await update.message.reply_text(
                f"Already using <code>{escape_html(new_model)}</code>.",
                parse_mode="HTML",
            )
            return

        self.settings.claude_model = new_model
        # Reset session — different model = different conversation
        _ud(context)["claude_session_id"] = None
        _ud(context)["force_new_session"] = True

        await update.message.reply_text(
            f"Model changed to <code>{escape_html(new_model)}</code>.\n"
            "Session reset — the new model will be used on your next message.",
            parse_mode="HTML",
        )

        audit_logger = _bd(context).get("audit_logger")
        if audit_logger and update.effective_user:
            await audit_logger.log_command(
                user_id=update.effective_user.id,
                command="model",
                args=[new_model],
                success=True,
            )

    async def agentic_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/settings — interactive settings menu (owner only)."""
        assert update.message is not None
        assert update.effective_user is not None

        from .settings_ui import build_menu_keyboard, is_owner

        if not is_owner(update.effective_user.id, self.settings):
            await update.message.reply_text("Owner only.")
            return

        kb = build_menu_keyboard()
        await update.message.reply_text("⚙️ Settings", reply_markup=kb)

        audit_logger = _bd(context).get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=update.effective_user.id,
                command="settings",
                args=[],
                success=True,
            )

    async def _settings_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle set: callbacks for the interactive /settings menu."""
        from . import settings_ui

        query = update.callback_query
        assert query is not None
        assert query.from_user is not None

        if not settings_ui.is_owner(query.from_user.id, self.settings):
            await query.answer("Owner only", show_alert=True)
            return

        await query.answer()

        data = query.data or ""
        parts = data.split(":")  # e.g. ["set","cat","claude"] or ["set","val","claude_model","claude-sonnet-4-6"]
        action = parts[1] if len(parts) > 1 else ""

        env_path = settings_ui.resolve_env_file()

        if action == "menu":
            kb = settings_ui.build_menu_keyboard()
            await query.edit_message_text("⚙️ Settings", reply_markup=kb)

        elif action == "cat":
            cat_key = parts[2] if len(parts) > 2 else ""
            cat = settings_ui.SETTINGS_CATEGORIES.get(cat_key, {})
            kb = settings_ui.build_category_keyboard(cat_key, self.settings)
            await query.edit_message_text(f"{cat.get('label', cat_key)} Settings", reply_markup=kb)

        elif action == "toggle":
            field = parts[2] if len(parts) > 2 else ""
            change = settings_ui.toggle_setting(self.settings, env_path, field)
            cat_key = settings_ui.find_category(field)
            cat = settings_ui.SETTINGS_CATEGORIES.get(cat_key, {})
            kb = settings_ui.build_category_keyboard(cat_key, self.settings)
            await query.edit_message_text(
                f"{cat.get('label', cat_key)} Settings\n<i>Changed: {change}</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )

        elif action == "choose":
            field = parts[2] if len(parts) > 2 else ""
            field_def = settings_ui.find_field(field)
            choices = field_def.get("choices", {}) if field_def else {}
            kb = settings_ui.build_choice_keyboard(field, choices)
            label = field_def["label"] if field_def else field
            await query.edit_message_text(f"Choose {label}:", reply_markup=kb)

        elif action == "val":
            # parts: ["set", "val", "field_name", "value"] — value may contain hyphens
            field = parts[2] if len(parts) > 2 else ""
            value = parts[3] if len(parts) > 3 else ""
            change = settings_ui.apply_setting(self.settings, env_path, field, value)
            # Reset session when model changes (different model = new conversation)
            if field == "claude_model":
                _ud(context)["claude_session_id"] = None
                _ud(context)["force_new_session"] = True
            cat_key = settings_ui.find_category(field)
            cat = settings_ui.SETTINGS_CATEGORIES.get(cat_key, {})
            kb = settings_ui.build_category_keyboard(cat_key, self.settings)
            await query.edit_message_text(
                f"{cat.get('label', cat_key)} Settings\n<i>Changed: {change}</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )

        elif action == "inc":
            field = parts[2] if len(parts) > 2 else ""
            settings_ui.increment_setting(self.settings, env_path, field, +1)
            cat_key = settings_ui.find_category(field)
            cat = settings_ui.SETTINGS_CATEGORIES.get(cat_key, {})
            kb = settings_ui.build_category_keyboard(cat_key, self.settings)
            await query.edit_message_text(f"{cat.get('label', cat_key)} Settings", reply_markup=kb)

        elif action == "dec":
            field = parts[2] if len(parts) > 2 else ""
            settings_ui.increment_setting(self.settings, env_path, field, -1)
            cat_key = settings_ui.find_category(field)
            cat = settings_ui.SETTINGS_CATEGORIES.get(cat_key, {})
            kb = settings_ui.build_category_keyboard(cat_key, self.settings)
            await query.edit_message_text(f"{cat.get('label', cat_key)} Settings", reply_markup=kb)

        # action == "noop": display-only button, nothing to do

    async def agentic_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/reload — restart the bot process in-place to pick up code/config changes."""
        import os
        import sys

        assert update.message is not None
        assert update.effective_user is not None

        audit_logger = _bd(context).get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=update.effective_user.id,
                command="reload",
                args=[],
                success=True,
            )

        await update.message.reply_text("Restarting bot process...")
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    async def agentic_repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        assert update.message is not None
        assert update.effective_user is not None
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory
        current_dir = _ud(context).get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            _ud(context)["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = _bd(context).get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(update.effective_user.id, target_path)
                if existing:
                    session_id = existing.session_id
            _ud(context)["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: list[str] = []
        keyboard_rows: list[list] = []
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def _agentic_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        assert query is not None
        assert query.from_user is not None
        assert query.data is not None
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        _ud(context)["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = _bd(context).get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(query.from_user.id, new_path)
            if existing:
                session_id = existing.session_id
        _ud(context)["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = _bd(context).get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
