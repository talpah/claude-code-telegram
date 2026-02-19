"""User profile management for Claude context enrichment.

Loads a markdown profile file (with mtime-based cache) and exposes
it as a formatted string to prepend to Claude's prompt.
"""

from pathlib import Path

import structlog

logger = structlog.get_logger()


class ProfileManager:
    """Load and cache a markdown user profile for prompt injection."""

    def __init__(self, profile_path: Path | None = None) -> None:
        self._profile_path = profile_path
        self._cache: str | None = None
        self._cache_mtime: float = 0.0

    def get_profile_context(self) -> str:
        """Load profile.md, cache by mtime. Return empty string if no profile."""
        if not self._profile_path:
            return ""
        try:
            if not self._profile_path.exists():
                return ""
            mtime = self._profile_path.stat().st_mtime
            if self._cache is None or mtime != self._cache_mtime:
                content = self._profile_path.read_text(encoding="utf-8").strip()
                self._cache = content
                self._cache_mtime = mtime
                logger.debug("User profile loaded", path=str(self._profile_path))
            return self._cache or ""
        except Exception as e:
            logger.warning("Failed to load user profile", path=str(self._profile_path), error=str(e))
            return ""
