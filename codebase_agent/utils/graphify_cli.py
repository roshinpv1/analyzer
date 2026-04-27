"""
Utility for interacting with the Graphify CLI.
"""
import subprocess
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

class GraphifyCLI:
    """
    Wrapper for Graphify CLI operations.
    """
    
    def __init__(self, codebase_path: str):
        self.codebase_path = Path(codebase_path).resolve()
        # Graphify v5 hardcodes output to <path>/graphify-out
        self.output_dir = self.codebase_path / "graphify-out"
        self.graph_json = self.output_dir / "graph.json"
        self.graph_report = self.output_dir / "GRAPH_REPORT.md"

    def is_indexed(self) -> bool:
        """Check if the codebase has already been indexed."""
        return self.graph_json.exists()

    def index(self, force: bool = False) -> bool:
        """
        Run the initial indexing pipeline.
        
        Args:
            force: If True, re-index even if graph already exists.
        """
        if self.is_indexed() and not force:
            logger.info("Codebase already indexed. Skipping.")
            return True

        logger.info(f"Indexing codebase at {self.codebase_path}...")
        
        # Ensure output directory exists (Graphify usually handles this but safety first)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        command = [
            sys.executable, "-m", "codebase_agent.graphify",
            "update", str(self.codebase_path)
        ]
        
        try:
            # We run this in the background or wait? 
            # For the initial indexing, we should wait as the analyzer needs the report.
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True
            )
            logger.info("Graphify indexing successful")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Graphify indexing failed (exit code {e.returncode})")
            if e.stdout:
                logger.error(f"STDOUT: {e.stdout}")
            if e.stderr:
                logger.error(f"STDERR: {e.stderr}")
            return False

    def update(self) -> bool:
        """Update the existing graph (incremental AST update)."""
        if not self.is_indexed():
            return self.index()
            
        logger.info("Updating graphify index...")
        command = [
            "python3", "-m", "codebase_agent.graphify",
            "update", str(self.codebase_path)
        ]
        
        try:
            subprocess.run(command, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Graphify update failed: {e.stderr}")
            return False

    def read_report(self) -> str | None:
        """Read the generated GRAPH_REPORT.md content."""
        if not self.graph_report.exists():
            return None
            
        try:
            return self.graph_report.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read graph report: {e}")
            return None
