"""
Tool for interacting with Graphify structural knowledge graph.

Uses in-process graph loading and ``runtime_dispatch`` so queries do not depend on a
separate ``python3`` interpreter or working-directory quirks.
"""
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


class GraphifyTool:
    """
    Integrates Graphify structural knowledge into the agent's workflow.
    """

    def __init__(self, codebase_path: str):
        self.codebase_path = Path(codebase_path).resolve()
        if self.codebase_path.name == "graphify-out":
            self.output_dir = self.codebase_path
        else:
            self.output_dir = self.codebase_path / "graphify-out"

        self.graph_json = self.output_dir / "graph.json"
        self.usage_stats: Dict[str, int] = {}
        self._graph: Any = None  # nx.Graph when loaded
        self._graph_failed: bool = False
        self._graph_load_err: str = ""

    def _get_graph(self):
        """Return loaded NetworkX graph, or None if missing or failed."""
        if self._graph is not None:
            return self._graph
        if self._graph_failed:
            return None
        if not self.graph_json.exists():
            return None
        try:
            from codebase_agent.graphify.runtime_dispatch import load_graph_nx

            self._graph = load_graph_nx(self.graph_json)
            return self._graph
        except Exception as e:
            logger.warning("GraphifyTool: failed to load %s: %s", self.graph_json, e)
            self._graph_failed = True
            self._graph_load_err = str(e)
            return None

    def query(self, question: str, mode: str = "bfs") -> str:
        """Search the knowledge graph using natural language (lexical + hub fallback)."""
        return self.execute_tool(
            "query_graph", {"question": question, "mode": mode, "depth": 3, "token_budget": 2000}
        )

    def get_path(self, source: str, target: str) -> str:
        """Find the shortest path between two concepts."""
        return self.execute_tool("shortest_path", {"source": source, "target": target})

    def explain(self, node_label: str) -> str:
        """Get detailed information and neighbors for a specific node."""
        return self.execute_tool("explain", {"label": node_label})

    def get_god_nodes(self) -> str:
        """Return the most central non-file hub nodes (same metric as MCP ``god_nodes``)."""
        return self.execute_tool("god_nodes", {"top_n": 10})

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """Dispatcher for agent-requested graph tools."""
        current_count = self.usage_stats.get(tool_name, 0)
        if current_count >= 30:
            logger.warning(
                "Graph tool %s has reached its session limit (30). Blocking further calls.",
                tool_name,
            )
            return (
                f"Error: The graph tool '{tool_name}' has reached its maximum session limit. "
                "Please proceed with the information you already have."
            )

        self.usage_stats[tool_name] = current_count + 1

        g = self._get_graph()
        if g is None:
            if not self.graph_json.exists():
                return (
                    f"Error: No graph index at {self.graph_json}. "
                    "Run indexing (analysis with graphify enabled) or from the repo root run: "
                    "`python -m codebase_agent.graphify update <codebase_path>`."
                )
            return f"Error: Could not load graph: {self._graph_load_err or 'unknown error'}"

        try:
            from codebase_agent.graphify.runtime_dispatch import dispatch_tool

            return dispatch_tool(g, tool_name, arguments)
        except Exception as e:
            logger.exception("GraphifyTool execute_tool failed: %s", tool_name)
            return f"Error executing graph tool {tool_name}: {e}"
