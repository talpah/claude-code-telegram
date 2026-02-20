"""Soul / identity layer for Claude's system prompt.

Loads a markdown soul file (with mtime-based cache) and exposes it as a
formatted string to prepend to Claude's system_prompt.
"""

from pathlib import Path

import structlog

logger = structlog.get_logger()


class SoulManager:
    """Load and cache a markdown soul file for system_prompt injection."""

    def __init__(self, soul_path: Path | None = None) -> None:
        self._soul_path = soul_path
        self._cache: str | None = None
        self._cache_mtime: float = 0.0

    def get_soul_content(self) -> str:
        """Load soul.md, cache by mtime. Return empty string if no soul file."""
        if not self._soul_path:
            return ""
        try:
            if not self._soul_path.exists():
                return ""
            mtime = self._soul_path.stat().st_mtime
            if self._cache is None or mtime != self._cache_mtime:
                content = self._soul_path.read_text(encoding="utf-8").strip()
                self._cache = content
                self._cache_mtime = mtime
                logger.debug("Soul file loaded", path=str(self._soul_path))
            return self._cache or ""
        except Exception as e:
            logger.warning("Failed to load soul file", path=str(self._soul_path), error=str(e))
            return ""
