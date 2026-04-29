import os
import re
import math
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import Counter

logger = logging.getLogger(__name__)

class CodebaseSearcher:
    """
    Pure Python implementation of BM25 and TF-IDF fuzzy search for codebase exploration.
    Maintains a lightweight inverted index of the codebase for fast retrieval.
    """

    def __init__(self, working_directory: str | Path):
        self.working_directory = Path(working_directory).resolve()
        self.is_indexed = False
        
        # Core data structures
        self.documents: Dict[str, str] = {}  # filepath -> full text
        self.doc_lengths: Dict[str, int] = {}  # filepath -> token count
        self.doc_term_freqs: Dict[str, Counter] = {}  # filepath -> Term Frequency Counter
        self.doc_freqs: Counter = Counter()  # term -> number of docs containing term
        
        self.avg_doc_len = 0.0
        self.total_docs = 0

        # BM25 Parameters
        self.k1 = 1.5
        self.b = 0.75

    def _tokenize(self, text: str) -> List[str]:
        """Simple regex-based tokenization splitting on non-alphanumeric chars."""
        # Convert to lowercase and split by non-alphanumeric
        return [t for t in re.split(r'[^a-zA-Z0-9]+', text.lower()) if len(t) > 1]

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
        """Scan and index all text files in the working directory."""
        if self.is_indexed:
            return

        logger.info(f"Building semantic index for {self.working_directory}...")
        total_len = 0
        
        for root, dirs, files in os.walk(self.working_directory):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if not any(skip in d for skip in ['.git', '__pycache__', 'node_modules', '.venv', '.env', 'build', 'dist', '.idea'])]
            
            for file in files:
                file_path = Path(root) / file
                
                # Skip known binary or heavy extensions
                if file_path.suffix.lower() in ['.pyc', '.png', '.jpg', '.jpeg', '.gif', '.zip', '.tar', '.gz', '.pdf', '.exe', '.dll', '.so']:
                    continue
                
                if self._is_binary(file_path):
                    continue

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        
                    rel_path = str(file_path.relative_to(self.working_directory))
                    self.documents[rel_path] = content
                    
                    tokens = self._tokenize(content)
                    self.doc_lengths[rel_path] = len(tokens)
                    total_len += len(tokens)
                    
                    term_counts = Counter(tokens)
                    self.doc_term_freqs[rel_path] = term_counts
                    
                    for term in term_counts.keys():
                        self.doc_freqs[term] += 1
                        
                except (UnicodeDecodeError, PermissionError):
                    continue

        self.total_docs = len(self.documents)
        if self.total_docs > 0:
            self.avg_doc_len = total_len / self.total_docs
            
        self.is_indexed = True
        logger.info(f"Indexing complete. Indexed {self.total_docs} files.")

    def _score_bm25(self, query_tokens: List[str], doc_id: str) -> float:
        """Calculate the BM25 score for a document given query tokens."""
        score = 0.0
        doc_len = self.doc_lengths[doc_id]
        term_freqs = self.doc_term_freqs[doc_id]

        for token in query_tokens:
            if token not in term_freqs:
                continue

            # Term frequency in document
            f = term_freqs[token]
            
            # Document frequency
            n = self.doc_freqs[token]
            
            # IDF calculation (BM25 variant)
            idf = math.log((self.total_docs - n + 0.5) / (n + 0.5) + 1.0)
            
            # TF-IDF calculation with saturation
            tf = (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * (doc_len / self.avg_doc_len)))
            
            score += idf * tf
            
        return score

    def _score_tfidf(self, query_tokens: List[str], doc_id: str) -> float:
        """Calculate the TF-IDF score for a document given query tokens."""
        score = 0.0
        doc_len = self.doc_lengths[doc_id]
        if doc_len == 0:
            return 0.0
            
        term_freqs = self.doc_term_freqs[doc_id]

        for token in query_tokens:
            if token not in term_freqs:
                continue

            # Term Frequency: count / doc_len
            tf = term_freqs[token] / doc_len
            
            # Inverse Document Frequency: log(N / n)
            n = self.doc_freqs[token]
            idf = math.log(self.total_docs / (1 + n)) + 1  # +1 smoothing
            
            score += tf * idf
            
        return score

    def search(self, query: str, algorithm: str = "bm25", top_k: int = 5) -> List[Tuple[str, float, str]]:
        """
        Search the codebase for the given query.
        
        Args:
            query: The search query string.
            algorithm: 'bm25' or 'tfidf'.
            top_k: Number of results to return.
            
        Returns:
            List of tuples (filepath, score, snippet).
        """
        if not self.is_indexed:
            self.build_index()

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores = []
        for doc_id in self.documents.keys():
            if algorithm.lower() == "tfidf":
                score = self._score_tfidf(query_tokens, doc_id)
            else:
                score = self._score_bm25(query_tokens, doc_id)
                
            if score > 0:
                scores.append((doc_id, score))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)
        top_results = scores[:top_k]

        # Extract snippets for context
        results_with_snippets = []
        for doc_id, score in top_results:
            snippet = self._extract_snippet(doc_id, query_tokens)
            results_with_snippets.append((doc_id, score, snippet))

        return results_with_snippets

    def _extract_snippet(self, doc_id: str, query_tokens: List[str]) -> str:
        """Extract a relevant snippet from the document."""
        text = self.documents[doc_id]
        lines = text.splitlines()
        
        best_line_idx = 0
        best_line_score = -1
        
        for i, line in enumerate(lines):
            line_tokens = set(self._tokenize(line))
            score = sum(1 for q in query_tokens if q in line_tokens)
            if score > best_line_score:
                best_line_score = score
                best_line_idx = i
                
        # Return +/- 2 lines around the best match
        start = max(0, best_line_idx - 2)
        end = min(len(lines), best_line_idx + 3)
        
        snippet = "\n".join([f"{idx+1}: {lines[idx].strip()}" for idx in range(start, end)])
        return snippet
