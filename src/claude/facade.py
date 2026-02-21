"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..config.settings import Settings
from .exceptions import ClaudeToolValidationError
from .monitor import ToolMonitor
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import ClaudeSession, SessionManager

if TYPE_CHECKING:
    from ..config.profile import ProfileManager
    from ..config.soul import SoulManager
    from ..memory.file_manager import MemoryFileManager
    from ..memory.manager import MemoryManager

logger = structlog.get_logger()


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: ClaudeSDKManager | None = None,
        session_manager: SessionManager | None = None,
        tool_monitor: ToolMonitor | None = None,
        profile_manager: "ProfileManager | None" = None,
        memory_manager: "MemoryManager | None" = None,
        soul_manager: "SoulManager | None" = None,
        memory_file_manager: "MemoryFileManager | None" = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(config)
        self.session_manager = session_manager
        self.tool_monitor = tool_monitor
        self.profile_manager = profile_manager
        self.memory_manager = memory_manager
        self.soul_manager = soul_manager
        self.memory_file_manager = memory_file_manager

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: str | None = None,
        on_stream: Callable[[StreamUpdate], None] | None = None,
        force_new: bool = False,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration."""
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
        )

        # If no session_id provided (or only a temp_* placeholder), try to find an
        # existing session for this user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if (not session_id or session_id.startswith("temp_")) and not force_new:
            existing_session = await self._find_resumable_session(user_id, working_directory)
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        assert self.session_manager is not None, "session_manager required"
        assert self.tool_monitor is not None, "tool_monitor required"
        session_manager = self.session_manager
        tool_monitor = self.tool_monitor

        # Get or create session
        session = await session_manager.get_or_create_session(user_id, working_directory, session_id)

        # Track streaming updates and validate tool calls
        tools_validated = True
        validation_errors = []
        blocked_tools = set()

        async def stream_handler(update: StreamUpdate):
            nonlocal tools_validated

            # Validate tool calls
            if update.tool_calls:
                for tool_call in update.tool_calls:
                    tool_name = tool_call["name"]
                    valid, error = await tool_monitor.validate_tool_call(
                        tool_name,
                        tool_call.get("input", {}),
                        working_directory,
                        user_id,
                    )

                    if not valid:
                        tools_validated = False
                        validation_errors.append(error)

                        # Track blocked tools
                        if error and "Tool not allowed:" in error:
                            blocked_tools.add(tool_name)

                        logger.error(
                            "Tool validation failed",
                            tool_name=tool_name,
                            error=error,
                            user_id=user_id,
                        )

                        # For critical tools, we should fail fast
                        if tool_name in ["Task", "Read", "Write", "Edit", "Bash"]:
                            # Create comprehensive error message
                            admin_instructions = self._get_admin_instructions(list(blocked_tools))
                            error_msg = self._create_tool_error_message(
                                list(blocked_tools),
                                self.config.claude_allowed_tools or [],
                                admin_instructions,
                            )

                            raise ClaudeToolValidationError(
                                error_msg,
                                blocked_tools=list(blocked_tools),
                                allowed_tools=self.config.claude_allowed_tools or [],
                            )

            # Pass to caller's handler
            if on_stream:
                try:
                    await on_stream(update)
                except Exception as e:
                    logger.warning("Stream callback failed", error=str(e))

        # Build enriched prompt (profile + memory + time context)
        # Pass the claude session_id so session notes are injected on resume
        enriched_prompt = await self._build_enriched_prompt(
            user_id=user_id,
            prompt=prompt,
            session_id=session_id,
        )

        # Execute command
        try:
            # Continue session if we have a real (non-temporary) session ID
            is_new = getattr(session, "is_new_session", False)
            has_real_session = not is_new and not session.session_id.startswith("temp_")
            should_continue = has_real_session

            # For new sessions, don't pass the temporary session_id to Claude Code
            claude_session_id = session.session_id if has_real_session else None

            try:
                response = await self._execute(
                    prompt=enriched_prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=stream_handler,
                )
            except Exception as resume_error:
                # If resume failed (e.g., session expired on Claude's side),
                # retry as a fresh session
                if should_continue and "no conversation found" in str(resume_error).lower():
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
                        error=str(resume_error),
                    )
                    # Clean up the stale session
                    await session_manager.remove_session(session.session_id)

                    # Create a fresh session and retry
                    session = await session_manager.get_or_create_session(user_id, working_directory)
                    response = await self._execute(
                        prompt=enriched_prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=stream_handler,
                    )
                else:
                    raise

            # Check if tool validation failed
            if not tools_validated:
                logger.error(
                    "Command completed but tool validation failed",
                    validation_errors=validation_errors,
                )
                # Mark response as having errors and include validation details
                response.is_error = True
                response.error_type = "tool_validation_failed"

                # Extract blocked tool names for user feedback
                blocked_tools = []
                for error in validation_errors:
                    if "Tool not allowed:" in error:
                        tool_name = error.split("Tool not allowed: ")[1]
                        blocked_tools.append(tool_name)

                # Create user-friendly error message
                if blocked_tools:
                    tool_list = ", ".join(f"`{tool}`" for tool in blocked_tools)
                    response.content = (
                        f"🚫 **Tool Access Blocked**\n\n"
                        f"Claude tried to use tools not allowed:\n"
                        f"{tool_list}\n\n"
                        f"**What you can do:**\n"
                        f"• Contact the administrator to request access to these tools\n"
                        f"• Try rephrasing your request to use different approaches\n"
                        f"• Check what tools are currently available with `/status`\n\n"
                        f"**Currently allowed tools:**\n"
                        f"{', '.join(f'`{t}`' for t in self.config.claude_allowed_tools or [])}"
                    )
                else:
                    response.content = (
                        f"🚫 **Tool Validation Failed**\n\n"
                        f"Tools failed security validation. Try different approach.\n\n"
                        f"Details: {'; '.join(validation_errors)}"
                    )

            # Update session (this may change the session_id for new sessions)
            old_session_id = session.session_id
            await session_manager.update_session(session.session_id, response)

            # For new sessions, use the session_id Claude assigned.
            # Use getattr (not hasattr) so that is_new_session=False means "continuing".
            if getattr(session, "is_new_session", False) and response.session_id:
                final_session_id = response.session_id
            else:
                final_session_id = old_session_id

            # Ensure response has the correct session_id.
            # Never propagate temp_* IDs to callers — they're not resumable and
            # storing them in user_data would prevent the auto-resume lookup from
            # running on the next message.
            if final_session_id and not final_session_id.startswith("temp_"):
                response.session_id = final_session_id
            else:
                response.session_id = ""

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: str | None = None,
        continue_session: bool = False,
        stream_callback: Callable | None = None,
    ) -> ClaudeResponse:
        """Execute command via SDK."""
        soul_content = self.soul_manager.get_soul_content() if self.soul_manager else None
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            soul_content=soul_content,
        )

    async def _build_enriched_prompt(
        self,
        user_id: int,
        prompt: str,
        session_id: str | None = None,
    ) -> str:
        """Prepend profile, memory, and time context to the user's prompt."""
        sections: list[str] = []

        # User profile
        if self.profile_manager:
            profile_context = self.profile_manager.get_profile_context()
            if profile_context:
                sections.append(profile_context)

        # Semantic memory context (SQLite-backed)
        if self.memory_manager:
            memory_context = await self.memory_manager.build_memory_context(user_id, query=prompt)
            if memory_context:
                sections.append(memory_context)

        # File-based long-term memory and notes
        if self.memory_file_manager:
            file_memory = self.memory_file_manager.get_memory_context()
            if file_memory:
                sections.append(f"## Long-Term Memory\n{file_memory}")
            notes = self.memory_file_manager.get_notes_context()
            if notes:
                sections.append(f"## Notes\n{notes}")

        # Session notes (injected on resume)
        if session_id and not session_id.startswith("temp_") and self.memory_manager:
            session_notes = await self.memory_manager.build_session_notes_context(session_id)
            if session_notes:
                sections.append(session_notes)

        # Language preference
        lang = getattr(self.config, "preferred_language", "auto")
        if lang and lang.lower() != "auto":
            sections.append(f"## Language\nAlways respond in {lang}, regardless of what language the user writes in.")
        else:
            sections.append(
                "## Language\nDetect the language the user writes in and respond in that same language. "
                "If they switch languages, follow their lead."
            )

        # Current time
        now = datetime.now(UTC)
        tz = getattr(self.config, "user_timezone", "UTC")
        sections.append(f"## Current Context\nTime: {now.strftime('%Y-%m-%d %H:%M')} UTC (Timezone: {tz})")

        # Memory tag instructions
        tag_instructions: list[str] = []
        if self.memory_manager:
            tag_instructions += [
                "When the user shares something worth remembering, include [REMEMBER: fact] in your response.",
                "To track a user goal, include [GOAL: objective].",
                "When a goal is completed, include [DONE: goal description].",
            ]
        if self.memory_file_manager:
            tag_instructions.append(
                "To save an important fact to persistent memory, include [MEMFILE: fact] in your response."
            )
        if self.memory_manager:
            tag_instructions.append(
                "To leave a note about what you're working on for session continuity, "
                "include [NOTE: text] in your response."
            )
        if tag_instructions:
            tag_instructions.append("These tags are extracted automatically — the user won't see them.")
            sections.append("## Memory Instructions\n" + "\n".join(tag_instructions))

        if not sections:
            return prompt

        return "\n\n".join(sections) + "\n\n---\n\n" + prompt

    async def quick_query(self, prompt: str, working_directory: Path) -> str:
        """Execute a one-shot query without session management. Returns response content."""
        response = await self._execute(
            prompt=prompt,
            working_directory=working_directory,
            session_id=None,
            continue_session=False,
            stream_callback=None,
        )
        return response.content

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> ClaudeSession | None:
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """

        assert self.session_manager is not None, "session_manager required"
        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and not s.session_id.startswith("temp_")
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: str | None = None,
        on_stream: Callable[[StreamUpdate], None] | None = None,
    ) -> ClaudeResponse | None:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        assert self.session_manager is not None, "session_manager required"
        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude temporary sessions)
        matching_sessions = [
            s for s in sessions if s.project_path == working_directory and not s.session_id.startswith("temp_")
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        """Get session information."""
        assert self.session_manager is not None, "session_manager required"
        return await self.session_manager.get_session_info(session_id)

    async def get_user_sessions(self, user_id: int) -> list[dict[str, Any]]:
        """Get all sessions for a user."""
        assert self.session_manager is not None, "session_manager required"
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        assert self.session_manager is not None, "session_manager required"
        return await self.session_manager.cleanup_expired_sessions()

    async def get_tool_stats(self) -> dict[str, Any]:
        """Get tool usage statistics."""
        assert self.tool_monitor is not None, "tool_monitor required"
        return self.tool_monitor.get_tool_stats()

    async def get_user_summary(self, user_id: int) -> dict[str, Any]:
        """Get comprehensive user summary."""
        assert self.session_manager is not None, "session_manager required"
        assert self.tool_monitor is not None, "tool_monitor required"
        session_summary = await self.session_manager.get_user_session_summary(user_id)
        tool_usage = self.tool_monitor.get_user_tool_usage(user_id)

        return {
            "user_id": user_id,
            **session_summary,
            **tool_usage,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        # Kill any active processes
        await self.sdk_manager.kill_all_processes()

        # Clean up expired sessions
        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")

    def _get_admin_instructions(self, blocked_tools: list[str]) -> str:
        """Generate admin instructions for enabling blocked tools."""
        instructions = []

        # Check if settings file exists
        settings_file = Path(".env")

        if blocked_tools:
            # Get current allowed tools and create merged list without duplicates
            current_tools = [
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
            ]
            merged_tools = list(
                dict.fromkeys(current_tools + blocked_tools)
            )  # Remove duplicates while preserving order
            merged_tools_str = ",".join(merged_tools)
            merged_tools_py = ", ".join(f'"{tool}"' for tool in merged_tools)

            instructions.append("**For Administrators:**")
            instructions.append("")

            if settings_file.exists():
                instructions.append("To enable these tools, add them to your `.env` file:")
                instructions.append("```")
                instructions.append(f'CLAUDE_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")
            else:
                instructions.append("To enable these tools:")
                instructions.append("1. Create a `.env` file in your project root")
                instructions.append("2. Add the following line:")
                instructions.append("```")
                instructions.append(f'CLAUDE_ALLOWED_TOOLS="{merged_tools_str}"')
                instructions.append("```")

            instructions.append("")
            instructions.append("Or modify the default in `src/config/settings.py`:")
            instructions.append("```python")
            instructions.append("claude_allowed_tools: Optional[List[str]] = Field(")
            instructions.append(f"    default=[{merged_tools_py}],")
            instructions.append('    description="List of allowed Claude tools",')
            instructions.append(")")
            instructions.append("```")

        return "\n".join(instructions)

    def _create_tool_error_message(
        self,
        blocked_tools: list[str],
        allowed_tools: list[str],
        admin_instructions: str,
    ) -> str:
        """Create a comprehensive error message for tool validation failures."""
        tool_list = ", ".join(f"`{tool}`" for tool in blocked_tools)
        allowed_list = ", ".join(f"`{tool}`" for tool in allowed_tools) if allowed_tools else "None"

        message = [
            "🚫 **Tool Access Blocked**",
            "",
            "Claude tried to use tools that are not currently allowed:",
            f"{tool_list}",
            "",
            "**Why this happened:**",
            "• Claude needs these tools to complete your request",
            "• These tools are not in the allowed tools list",
            "• This is a security feature to control what Claude can do",
            "",
            "**What you can do:**",
            "• Contact the administrator to request access to these tools",
            "• Try rephrasing your request to use different approaches",
            "• Use simpler requests that don't require these tools",
            "",
            "**Currently allowed tools:**",
            f"{allowed_list}",
            "",
            admin_instructions,
        ]

        return "\n".join(message)
