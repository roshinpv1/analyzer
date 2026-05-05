import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

class MemoryTool:
    """
    SQLite-backed memory tool with keyword-based retrieval.
    No embeddings/vector calls are required.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the MemoryTool.

        Args:
            db_path: Path to SQLite database file.
        """
        self.logger = logging.getLogger(__name__)
        resolved_path = Path(db_path or ".agent_memory.db").resolve()
        self.db_path = resolved_path
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        metadata TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_user_id ON memories(user_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at)"
                )
                conn.commit()
            self.enabled = True
            self.logger.info(f"MemoryTool initialized with SQLite at {self.db_path}")
        except Exception as e:
            self.logger.error(f"Failed to initialize MemoryTool: {e}")
            self.enabled = False

    def add_memory(self, content: str, user_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Add a piece of information to the memory.

        Args:
            content: The text content to remember.
            user_id: Unique identifier for the user or session.
            metadata: Optional additional context.

        Returns:
            True if successful, False otherwise.
        """
        if not self.enabled:
            return False

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO memories(user_id, content, metadata, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        content,
                        json.dumps(metadata or {}),
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Error adding to memory: {e}")
            return False

    def search_memory(self, query: str, user_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Search for relevant information in the memory.

        Args:
            query: The search query.
            user_id: Unique identifier for the user or session.
            limit: Maximum number of results to return.

        Returns:
            List of relevant memories with scores.
        """
        if not self.enabled:
            return []

        try:
            like_pattern = f"%{query}%"
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT content, metadata, created_at
                    FROM memories
                    WHERE user_id = ?
                      AND content LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (user_id, like_pattern, limit),
                ).fetchall()
            results: List[Dict[str, Any]] = []
            for row in rows:
                metadata_raw = row["metadata"] or "{}"
                try:
                    metadata = json.loads(metadata_raw)
                except Exception:
                    metadata = {}
                results.append(
                    {
                        "memory": row["content"],
                        "metadata": metadata,
                        "created_at": row["created_at"],
                    }
                )
            return results
        except Exception as e:
            self.logger.error(f"Error searching memory: {e}")
            return []

    def delete_memory(self, user_id: str) -> bool:
        """
        Delete all memories associated with a user/session.

        Args:
            user_id: Unique identifier for the user or session.

        Returns:
            True if successful, False otherwise.
        """
        if not self.enabled:
            return False

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
                conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Error deleting memory: {e}")
            return False
