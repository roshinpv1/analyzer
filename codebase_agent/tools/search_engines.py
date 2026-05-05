import os
import sqlite3
import logging
import re
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

class CodebaseSearcher:
    """
    SQLite FTS5 implementation of BM25 fuzzy search for codebase exploration.
    Maintains a highly scalable, compressed on-disk index of the codebase for fast retrieval.
    """

    def __init__(self, working_directory: str | Path):
        self.working_directory = Path(working_directory).resolve()
        
        # We store the DB in the working directory so it's persistent per-codebase
        self.db_path = self.working_directory / ".codebase_index.db"
        self.is_indexed = self.db_path.exists()
        self.total_docs = 0
        
        if self.is_indexed:
            # Quickly fetch document count to verify it's a valid index
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT count(*) FROM codebase")
                    self.total_docs = cursor.fetchone()[0]
            except Exception:
                self.is_indexed = False

    def _is_binary(self, filepath: Path) -> bool:
        """Check if a file is binary."""
        try:
            with open(filepath, 'rb') as f:
                chunk = f.read(1024)
                if b'\0' in chunk:
                    return True
        except Exception:
            return True
        return False

    def build_index(self) -> None:
        """Scan and index all text files in the working directory using SQLite FTS5."""
        if self.is_indexed:
            logger.info(f"Using existing SQLite FTS5 index at {self.db_path}")
            return

        logger.info(f"Building SQLite FTS5 index for {self.working_directory}...")
        
        # Connect to SQLite (creates the file if it doesn't exist)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # Create the FTS5 virtual table. We use unicode61 tokenizer for good text/code support.
            cursor.execute("DROP TABLE IF EXISTS codebase")
            cursor.execute('''
                CREATE VIRTUAL TABLE codebase USING fts5(
                    filepath, 
                    content, 
                    tokenize='unicode61 remove_diacritics 1'
                )
            ''')
            
            docs_to_insert = []
            
            for root, dirs, files in os.walk(self.working_directory):
                # Skip ignored directories
                dirs[:] = [d for d in dirs if not any(skip in d for skip in ['.git', '__pycache__', 'node_modules', '.venv', '.env', 'build', 'dist', '.idea', 'graphify-out'])]
                
                for file in files:
                    file_path = Path(root) / file
                    
                    # Skip known binary or heavy extensions
                    if file_path.suffix.lower() in ['.pyc', '.png', '.jpg', '.jpeg', '.gif', '.zip', '.tar', '.gz', '.pdf', '.exe', '.dll', '.so', '.db', '.sqlite', '.sqlite3']:
                        continue
                    
                    if self._is_binary(file_path):
                        continue

                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            
                        rel_path = str(file_path.relative_to(self.working_directory))
                        docs_to_insert.append((rel_path, content))
                        
                    except (UnicodeDecodeError, PermissionError):
                        continue

            # Batch insert for extreme speed
            cursor.executemany("INSERT INTO codebase (filepath, content) VALUES (?, ?)", docs_to_insert)
            conn.commit()
            
            self.total_docs = len(docs_to_insert)
            
        self.is_indexed = True
        logger.info(f"Indexing complete. SQLite index written to {self.db_path} with {self.total_docs} files.")

    def search(self, query: str, algorithm: str = "bm25", top_k: int = 5) -> List[Tuple[str, float, str]]:
        """
        Search the codebase for the given query using FTS5 MATCH.
        
        Args:
            query: The search query string.
            algorithm: Ignored (SQLite FTS5 natively calculates BM25).
            top_k: Number of results to return.
            
        Returns:
            List of tuples (filepath, score, snippet).
        """
        if not self.is_indexed:
            self.build_index()

        # Extract alphanumeric words to build a safe MATCH query
        words = [w for w in re.split(r'[^a-zA-Z0-9]+', query) if len(w) > 1]
        if not words:
            return []
            
        # Join with OR for fuzzy matching behavior
        fts_query = " OR ".join(words)
        
        results_with_snippets = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # FTS5 uses a hidden 'rank' column which is the negative BM25 score
                # The 'snippet' function creates a highly optimized highlighted snippet
                sql = """
                    SELECT 
                        filepath, 
                        snippet(codebase, 1, '>>', '<<', '...', 20) as snip,
                        rank
                    FROM codebase 
                    WHERE codebase MATCH ? 
                    ORDER BY rank 
                    LIMIT ?
                """
                
                cursor.execute(sql, (fts_query, top_k))
                rows = cursor.fetchall()
                
                for row in rows:
                    filepath, snip, rank = row
                    # FTS5 rank is negative (smaller is better). We negate it to make it positive.
                    score = -rank
                    results_with_snippets.append((filepath, score, snip))
                    
        except Exception as e:
            logger.error(f"SQLite FTS5 search failed: {e}")
            
        return results_with_snippets
