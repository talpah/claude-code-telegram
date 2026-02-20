"""Curated memory file and notes directory manager.

Reads a long-term memory.md file and per-topic notes/*.md files, providing
their content for injection into Claude's enriched prompt. Also supports
appending timestamped entries to memory.md via [MEMFILE:] tags.
"""

from datetime import UTC, datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()


class MemoryFileManager:
    """Manage a curated memory.md file and a notes/ directory."""

    def __init__(
        self,
        memory_path: Path | None = None,
        notes_dir: Path | None = None,
    ) -> None:
        self._memory_path = memory_path
        self._notes_dir = notes_dir
        self._memory_cache: str | None = None
        self._memory_mtime: float = 0.0

    def get_memory_context(self) -> str:
        """Return the contents of memory.md (mtime-cached)."""
        if not self._memory_path:
            return ""
        try:
            if not self._memory_path.exists():
                return ""
            mtime = self._memory_path.stat().st_mtime
            if self._memory_cache is None or mtime != self._memory_mtime:
                content = self._memory_path.read_text(encoding="utf-8").strip()
                self._memory_cache = content
                self._memory_mtime = mtime
                logger.debug("Memory file loaded", path=str(self._memory_path))
            return self._memory_cache or ""
        except Exception as e:
            logger.warning("Failed to load memory file", path=str(self._memory_path), error=str(e))
            return ""

    def get_notes_context(self) -> str:
        """Return concatenated contents of all *.md files in notes_dir."""
        if not self._notes_dir or not self._notes_dir.exists():
            return ""
        try:
            parts: list[str] = []
            for note_file in sorted(self._notes_dir.glob("*.md")):
                try:
                    content = note_file.read_text(encoding="utf-8").strip()
                    if content:
                        parts.append(f"### {note_file.stem}\n{content}")
                except Exception as e:
                    logger.warning("Failed to read note file", path=str(note_file), error=str(e))
            return "\n\n".join(parts)
        except Exception as e:
            logger.warning("Failed to read notes directory", path=str(self._notes_dir), error=str(e))
            return ""

    def append_entry(self, content: str) -> None:
        """Append a timestamped entry to memory.md and invalidate the cache."""
        if not self._memory_path:
            return
        try:
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
            entry = f"\n- [{timestamp}] {content}\n"
            with self._memory_path.open("a", encoding="utf-8") as f:
                f.write(entry)
            # Invalidate cache so next read picks up the new entry
            self._memory_cache = None
            self._memory_mtime = 0.0
            logger.debug("Memory entry appended", path=str(self._memory_path))
        except Exception as e:
            logger.warning("Failed to append memory entry", path=str(self._memory_path), error=str(e))
