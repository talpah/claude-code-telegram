"""Monitor Claude's tool usage.

Features:
- Track tool calls
- Security validation
- Usage analytics
- Bash directory boundary enforcement
"""

import shlex
from collections import defaultdict
from pathlib import Path
from typing import Any

import structlog

from ..config.settings import Settings
from ..security.validators import SecurityValidator

logger = structlog.get_logger()

# Commands that modify the filesystem and should have paths checked
_FS_MODIFYING_COMMANDS: set[str] = {
    "mkdir",
    "touch",
    "cp",
    "mv",
    "rm",
    "rmdir",
    "ln",
    "install",
    "tee",
}

# Commands that are read-only or don't take filesystem paths
_READ_ONLY_COMMANDS: set[str] = {
    "cat",
    "ls",
    "head",
    "tail",
    "less",
    "more",
    "which",
    "whoami",
    "pwd",
    "echo",
    "printf",
    "env",
    "printenv",
    "date",
    "wc",
    "sort",
    "uniq",
    "diff",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "realpath",
    "dirname",
    "basename",
}

# Actions / expressions that make ``find`` a filesystem-modifying command
_FIND_MUTATING_ACTIONS: set[str] = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}


def check_bash_directory_boundary(
    command: str,
    working_directory: Path,
    approved_directories: list[Path],
) -> tuple[bool, str | None]:
    """Check if a bash command's absolute paths stay within allowed directories.

    Returns (True, None) if the command is safe, or (False, error_message) if it
    attempts to write outside all approved directory boundaries.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # If we can't parse the command, let it through —
        # the sandbox will catch it at the OS level
        return True, None

    if not tokens:
        return True, None

    base_command = Path(tokens[0]).name

    # Read-only commands are always allowed
    if base_command in _READ_ONLY_COMMANDS:
        return True, None

    # Handle ``find`` specially: only dangerous when it contains mutating actions
    if base_command == "find":
        has_mutating_action = any(t in _FIND_MUTATING_ACTIONS for t in tokens[1:])
        if not has_mutating_action:
            return True, None
        # Fall through to path checking below
    elif base_command not in _FS_MODIFYING_COMMANDS:
        # Only check filesystem-modifying commands
        return True, None

    # Resolve all approved directories once
    resolved_approved = [d.resolve() for d in approved_directories]

    for token in tokens[1:]:
        # Skip flags
        if token.startswith("-"):
            continue

        # Resolve both absolute and relative paths against the working
        # directory so that traversal sequences like ``../../evil`` are
        # caught instead of being silently allowed.
        if token.startswith("/"):
            resolved = Path(token).resolve()
        else:
            resolved = (working_directory / token).resolve()

        in_any_approved = any(_path_within(resolved, approved) for approved in resolved_approved)
        if not in_any_approved:
            return False, (
                f"Directory boundary violation: '{base_command}' targets "
                f"'{token}' which is outside approved directories"
            )

    return True, None


def _path_within(path: Path, directory: Path) -> bool:
    """Return True if *path* is within *directory*."""
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


class ToolMonitor:
    """Monitor and validate Claude's tool usage."""

    def __init__(
        self,
        config: Settings,
        security_validator: SecurityValidator | None = None,
        agentic_mode: bool = False,
    ):
        """Initialize tool monitor."""
        self.config = config
        self.security_validator = security_validator
        self.agentic_mode = agentic_mode
        self.tool_usage: dict[str, int] = defaultdict(int)
        self.security_violations: list[dict[str, Any]] = []
        self.disable_tool_validation = getattr(config, "disable_tool_validation", False)

    async def validate_tool_call(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        working_directory: Path,
        user_id: int,
    ) -> tuple[bool, str | None]:
        """Validate tool call before execution."""
        logger.debug(
            "Validating tool call",
            tool_name=tool_name,
            working_directory=str(working_directory),
            user_id=user_id,
        )

        # When disabled, skip only allowlist/disallowlist name checks.
        # Keep path and command safety validation active.
        if self.disable_tool_validation:
            logger.debug(
                "Tool name validation disabled; skipping allow/disallow checks",
                tool_name=tool_name,
                user_id=user_id,
            )

        # Check if tool is allowed
        if (
            not self.disable_tool_validation
            and hasattr(self.config, "claude_allowed_tools")
            and self.config.claude_allowed_tools
        ):
            if tool_name not in self.config.claude_allowed_tools:
                violation = {
                    "type": "disallowed_tool",
                    "tool_name": tool_name,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                }
                self.security_violations.append(violation)
                logger.warning("Tool not allowed", **violation)
                return False, f"Tool not allowed: {tool_name}"

        # Check if tool is explicitly disallowed
        if (
            not self.disable_tool_validation
            and hasattr(self.config, "claude_disallowed_tools")
            and self.config.claude_disallowed_tools
        ):
            if tool_name in self.config.claude_disallowed_tools:
                violation = {
                    "type": "explicitly_disallowed_tool",
                    "tool_name": tool_name,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                }
                self.security_violations.append(violation)
                logger.warning("Tool explicitly disallowed", **violation)
                return False, f"Tool explicitly disallowed: {tool_name}"

        # Validate file operations
        if tool_name in [
            "create_file",
            "edit_file",
            "read_file",
            "Write",
            "Edit",
            "Read",
        ]:
            file_path = tool_input.get("path") or tool_input.get("file_path")
            if not file_path:
                return False, "File path required"

            # Validate path security
            if self.security_validator:
                valid, resolved_path, error = self.security_validator.validate_path(file_path, working_directory)

                if not valid:
                    violation = {
                        "type": "invalid_file_path",
                        "tool_name": tool_name,
                        "file_path": file_path,
                        "user_id": user_id,
                        "working_directory": str(working_directory),
                        "error": error,
                    }
                    self.security_violations.append(violation)
                    logger.warning("Invalid file path in tool call", **violation)
                    return False, error

        # Validate shell commands (skip in agentic mode — Claude Code runs
        # inside its own sandbox, and these patterns block normal gh/git usage)
        if tool_name in ["bash", "shell", "Bash"] and not self.agentic_mode:
            command = tool_input.get("command", "")

            # Check for dangerous commands
            dangerous_patterns = [
                "rm -rf",
                "sudo",
                "chmod 777",
                "curl",
                "wget",
                "nc ",
                "netcat",
                ">",
                ">>",
                "|",
                "&",
                ";",
                "$(",
                "`",
            ]

            for pattern in dangerous_patterns:
                if pattern in command.lower():
                    violation = {
                        "type": "dangerous_command",
                        "tool_name": tool_name,
                        "command": command,
                        "pattern": pattern,
                        "user_id": user_id,
                        "working_directory": str(working_directory),
                    }
                    self.security_violations.append(violation)
                    logger.warning("Dangerous command detected", **violation)
                    return False, f"Dangerous command pattern detected: {pattern}"

            # Check directory boundary for filesystem-modifying commands
            allowed_dirs = (
                self.config.all_allowed_paths
                if hasattr(self.config, "all_allowed_paths")
                else [self.config.approved_directory]
            )
            valid, error = check_bash_directory_boundary(command, working_directory, allowed_dirs)
            if not valid:
                violation = {
                    "type": "directory_boundary_violation",
                    "tool_name": tool_name,
                    "command": command,
                    "user_id": user_id,
                    "working_directory": str(working_directory),
                    "error": error,
                }
                self.security_violations.append(violation)
                logger.warning("Directory boundary violation", **violation)
                return False, error

        # Track usage
        self.tool_usage[tool_name] += 1

        logger.debug("Tool call validated successfully", tool_name=tool_name)
        return True, None

    def get_tool_stats(self) -> dict[str, Any]:
        """Get tool usage statistics."""
        return {
            "total_calls": sum(self.tool_usage.values()),
            "by_tool": dict(self.tool_usage),
            "unique_tools": len(self.tool_usage),
            "security_violations": len(self.security_violations),
        }

    def get_security_violations(self) -> list[dict[str, Any]]:
        """Get security violations."""
        return self.security_violations.copy()

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.tool_usage.clear()
        self.security_violations.clear()
        logger.info("Tool monitor statistics reset")

    def get_user_tool_usage(self, user_id: int) -> dict[str, Any]:
        """Get tool usage for specific user."""
        user_violations = [v for v in self.security_violations if v.get("user_id") == user_id]

        return {
            "user_id": user_id,
            "security_violations": len(user_violations),
            "violation_types": list(set(v.get("type") for v in user_violations)),
        }

    def is_tool_allowed(self, tool_name: str) -> bool:
        """Check if tool is allowed without validation."""
        # Check allowed list
        if hasattr(self.config, "claude_allowed_tools") and self.config.claude_allowed_tools:
            if tool_name not in self.config.claude_allowed_tools:
                return False

        # Check disallowed list
        if hasattr(self.config, "claude_disallowed_tools") and self.config.claude_disallowed_tools:
            if tool_name in self.config.claude_disallowed_tools:
                return False

        return True
