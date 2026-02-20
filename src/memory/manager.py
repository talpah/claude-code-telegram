"""Semantic memory manager for persistent user facts and goals.

Features:
- Store facts, goals, and preferences per user
- FTS5 full-text search with optional vector similarity fallback
- Tag extraction from Claude responses: [REMEMBER: ...], [GOAL: ...], [DONE: ...]
- Memory context injection into Claude prompts
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from ..storage.database import DatabaseManager

logger = structlog.get_logger()

_REMEMBER_RE = re.compile(r"\[REMEMBER:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_GOAL_RE = re.compile(r"\[GOAL:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_DONE_RE = re.compile(r"\[DONE:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_MEMFILE_RE = re.compile(r"\[MEMFILE:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_NOTE_RE = re.compile(r"\[NOTE:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)


@dataclass
class MemoryEntry:
    """A single persisted memory item."""

    id: int
    user_id: int
    entry_type: str  # 'fact', 'goal', 'preference'
    content: str
    deadline: str | None
    priority: int
    is_active: bool
    completed_at: datetime | None
    embedding: bytes | None
    created_at: datetime
    updated_at: datetime


@dataclass
class MemoryIntent:
    """Extracted intent from a Claude response."""

    action: str  # 'remember', 'goal', 'done', 'memfile', 'note'
    content: str


class MemoryManager:
    """Manage persistent semantic memory for users."""

    def __init__(self, db_manager: DatabaseManager, enable_embeddings: bool = True) -> None:
        self.db_manager = db_manager
        self.enable_embeddings = enable_embeddings

    # --- Write operations ---

    async def store_fact(self, user_id: int, content: str) -> int:
        """Store a fact about the user. Returns the new entry ID."""
        embedding = self._encode(content)
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO memory_entries (user_id, entry_type, content, embedding) VALUES (?, 'fact', ?, ?)",
                (user_id, content, embedding),
            )
            await conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def store_goal(self, user_id: int, content: str, deadline: str | None = None) -> int:
        """Store a user goal. Returns the new entry ID."""
        embedding = self._encode(content)
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO memory_entries (user_id, entry_type, content, deadline, embedding)
                VALUES (?, 'goal', ?, ?, ?)
                """,
                (user_id, content, deadline, embedding),
            )
            await conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    async def complete_goal(self, user_id: int, goal_content_fragment: str) -> bool:
        """Mark a goal as completed by partial content match. Returns True if any row updated."""
        now = datetime.now(UTC)
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE memory_entries
                SET is_active = 0, completed_at = ?
                WHERE user_id = ? AND entry_type = 'goal' AND is_active = 1 AND content LIKE ?
                """,
                (now, user_id, f"%{goal_content_fragment}%"),
            )
            await conn.commit()
            return (cursor.rowcount or 0) > 0

    # --- Read operations ---

    async def get_facts(self, user_id: int, limit: int = 20) -> list[MemoryEntry]:
        """Get active facts for a user, ordered by priority then recency."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM memory_entries
                WHERE user_id = ? AND entry_type = 'fact' AND is_active = 1
                ORDER BY priority DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_entry(row) for row in rows]

    async def get_active_goals(self, user_id: int) -> list[MemoryEntry]:
        """Get active goals for a user, ordered by priority then creation time."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM memory_entries
                WHERE user_id = ? AND entry_type = 'goal' AND is_active = 1
                ORDER BY priority DESC, created_at DESC
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_entry(row) for row in rows]

    async def search(self, user_id: int, query: str, limit: int = 5) -> list[MemoryEntry]:
        """Hybrid search: FTS5 text matching + optional vector similarity re-ranking."""
        results = await self._fts_search(user_id, query, limit)

        if self.enable_embeddings and len(results) < limit:
            query_emb = self._encode(query)
            if query_emb:
                vector_results = await self._vector_search(user_id, query_emb, limit)
                seen_ids = {e.id for e in results}
                for entry in vector_results:
                    if entry.id not in seen_ids:
                        results.append(entry)
                        seen_ids.add(entry.id)

        return results[:limit]

    async def build_memory_context(self, user_id: int, query: str | None = None) -> str:
        """Build a formatted memory context block to prepend to Claude's prompt."""
        facts = await self.get_facts(user_id, limit=20)
        goals = await self.get_active_goals(user_id)

        if query:
            relevant = await self.search(user_id, query, limit=5)
            fact_ids = {e.id for e in facts}
            goal_ids = {e.id for e in goals}
            for entry in relevant:
                if entry.entry_type == "fact" and entry.id not in fact_ids:
                    facts.append(entry)
                elif entry.entry_type == "goal" and entry.id not in goal_ids:
                    goals.append(entry)

        if not facts and not goals:
            return ""

        sections = ["## Your Memory\nYou have persistent memory. Here's what you know about this user:"]

        if facts:
            sections.append("### Facts")
            for fact in facts[:10]:
                sections.append(f"- {fact.content}")

        if goals:
            sections.append("### Active Goals")
            for goal in goals:
                deadline_str = f" (deadline: {goal.deadline})" if goal.deadline else ""
                sections.append(f"- {goal.content}{deadline_str}")

        return "\n".join(sections)

    # --- Response processing ---

    def extract_memory_intents(self, response_text: str) -> list[MemoryIntent]:
        """Scan Claude's response for [REMEMBER:], [GOAL:], [DONE:], [MEMFILE:], [NOTE:] tags."""
        intents: list[MemoryIntent] = []
        for match in _REMEMBER_RE.finditer(response_text):
            intents.append(MemoryIntent(action="remember", content=match.group(1).strip()))
        for match in _GOAL_RE.finditer(response_text):
            intents.append(MemoryIntent(action="goal", content=match.group(1).strip()))
        for match in _DONE_RE.finditer(response_text):
            intents.append(MemoryIntent(action="done", content=match.group(1).strip()))
        for match in _MEMFILE_RE.finditer(response_text):
            intents.append(MemoryIntent(action="memfile", content=match.group(1).strip()))
        for match in _NOTE_RE.finditer(response_text):
            intents.append(MemoryIntent(action="note", content=match.group(1).strip()))
        return intents

    async def process_response(
        self,
        user_id: int,
        response_text: str,
        memory_file_manager: "Any | None" = None,
        session_id: str | None = None,
    ) -> list[str]:
        """Extract and process memory intents from a Claude response. Returns descriptions of actions taken."""
        intents = self.extract_memory_intents(response_text)
        processed: list[str] = []
        for intent in intents:
            try:
                if intent.action == "remember":
                    await self.store_fact(user_id, intent.content)
                    processed.append(f"remembered: {intent.content[:50]}")
                elif intent.action == "goal":
                    await self.store_goal(user_id, intent.content)
                    processed.append(f"goal: {intent.content[:50]}")
                elif intent.action == "done":
                    completed = await self.complete_goal(user_id, intent.content)
                    if completed:
                        processed.append(f"completed: {intent.content[:50]}")
                elif intent.action == "memfile":
                    if memory_file_manager is not None:
                        memory_file_manager.append_entry(intent.content)
                        processed.append(f"memfile: {intent.content[:50]}")
                elif intent.action == "note":
                    if session_id and not session_id.startswith("temp_"):
                        await self._store_session_note(user_id, session_id, intent.content)
                        processed.append(f"note: {intent.content[:50]}")
            except Exception as e:
                logger.warning("Failed to process memory intent", action=intent.action, error=str(e))
        return processed

    async def _store_session_note(self, user_id: int, session_id: str, content: str) -> None:
        """Persist a session note to the database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                "INSERT INTO session_notes (user_id, session_id, content) VALUES (?, ?, ?)",
                (user_id, session_id, content),
            )
            await conn.commit()

    async def build_session_notes_context(self, session_id: str) -> str:
        """Build a formatted block of session notes for the given session."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT content, created_at FROM session_notes WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            )
            rows = await cursor.fetchall()

        if not rows:
            return ""

        lines = ["## Session Notes\nNotes from previous turns in this session:"]
        for row in rows:
            d = dict(row)
            lines.append(f"- {d['content']}")
        return "\n".join(lines)

    # --- Private helpers ---

    async def _fts_search(self, user_id: int, query: str, limit: int) -> list[MemoryEntry]:
        """FTS5 full-text search with LIKE fallback."""
        async with self.db_manager.get_connection() as conn:
            try:
                cursor = await conn.execute(
                    """
                    SELECT me.* FROM memory_entries me
                    JOIN memory_fts ON me.id = memory_fts.rowid
                    WHERE me.user_id = ? AND memory_fts MATCH ? AND me.is_active = 1
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (user_id, query, limit),
                )
                rows = await cursor.fetchall()
                return [self._row_to_entry(row) for row in rows]
            except Exception:
                cursor = await conn.execute(
                    """
                    SELECT * FROM memory_entries
                    WHERE user_id = ? AND is_active = 1 AND content LIKE ?
                    ORDER BY priority DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, f"%{query}%", limit),
                )
                rows = await cursor.fetchall()
                return [self._row_to_entry(row) for row in rows]

    async def _vector_search(self, user_id: int, query_embedding: bytes, limit: int) -> list[MemoryEntry]:
        """Find entries by cosine similarity to query embedding."""
        try:
            from .embeddings import EmbeddingService

            async with self.db_manager.get_connection() as conn:
                cursor = await conn.execute(
                    "SELECT * FROM memory_entries WHERE user_id = ? AND is_active = 1 AND embedding IS NOT NULL",
                    (user_id,),
                )
                rows = await cursor.fetchall()

            if not rows:
                return []

            scored: list[tuple[float, MemoryEntry]] = []
            for row in rows:
                entry = self._row_to_entry(row)
                if entry.embedding:
                    sim = EmbeddingService.similarity(query_embedding, entry.embedding)
                    scored.append((sim, entry))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [entry for _, entry in scored[:limit]]
        except Exception as e:
            logger.warning("Vector search failed", error=str(e))
            return []

    def _encode(self, text: str) -> bytes | None:
        """Encode text to embedding bytes. Returns None if embeddings disabled or unavailable."""
        if not self.enable_embeddings:
            return None
        try:
            from .embeddings import EmbeddingService

            return EmbeddingService.encode(text)
        except Exception as e:
            logger.debug("Embedding encoding skipped", error=str(e))
            return None

    @staticmethod
    def _row_to_entry(row: Any) -> MemoryEntry:
        """Convert a database row to a MemoryEntry dataclass."""
        d = dict(row)

        def _parse_dt(val: Any) -> datetime | None:
            if val is None:
                return None
            if isinstance(val, datetime):
                return val
            if isinstance(val, str):
                return datetime.fromisoformat(val)
            return None

        return MemoryEntry(
            id=d["id"],
            user_id=d["user_id"],
            entry_type=d["entry_type"],
            content=d["content"],
            deadline=d.get("deadline"),
            priority=d.get("priority", 0),
            is_active=bool(d.get("is_active", True)),
            completed_at=_parse_dt(d.get("completed_at")),
            embedding=d.get("embedding"),
            created_at=_parse_dt(d.get("created_at")) or datetime.now(UTC),
            updated_at=_parse_dt(d.get("updated_at")) or datetime.now(UTC),
        )
