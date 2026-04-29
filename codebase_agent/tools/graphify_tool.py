"""
Tool for interacting with Graphify structural knowledge graph.
Provides both CLI-based one-off queries and MCP-based structured access.
"""
import subprocess
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

class GraphifyTool:
    """
    Integrates Graphify structural knowledge into the agent's workflow.
    """
    
    def __init__(self, codebase_path: str, output_dir: str = "graphify-out"):
        self.codebase_path = Path(codebase_path).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.graph_json = self.output_dir / "graph.json"
        self.usage_stats: Dict[str, int] = {}
        
    def _run_cli(self, subcommand: str, *args: str) -> str:
        """Run a graphify CLI command and return output."""
        cmd = [
            "python3", "-m", "codebase_agent.graphify",
            subcommand, *args,
            "--graph", str(self.graph_json)
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            return f"Error running graphify {subcommand}: {e.stderr or str(e)}"

    def query(self, question: str, mode: str = "bfs") -> str:
        """
        Search the knowledge graph using natural language.
        
        Args:
            question: The natural language question or keyword.
            mode: 'bfs' for broad context, 'dfs' for deep tracing.
        """
        args = [question]
        if mode == "dfs":
            args.append("--dfs")
        return self._run_cli("query", *args)

    def get_path(self, source: str, target: str) -> str:
        """
        Find the shortest path between two concepts.
        """
        return self._run_cli("path", source, target)

    def explain(self, node_label: str) -> str:
        """
        Get detailed information and neighbors for a specific node.
        """
        return self._run_cli("explain", node_label)

    def get_god_nodes(self) -> str:
        """
        Identify the most central/connected nodes in the system.
        """
        # Note: God nodes is a report feature or can be queried via 'explain' on top nodes
        return self._run_cli("query", "god nodes")

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        """
        Dispatcher for agent-requested graph tools.
        """
        self.usage_stats[tool_name] = self.usage_stats.get(tool_name, 0) + 1
        if tool_name == "query_graph":
            return self.query(arguments.get("question", ""), arguments.get("mode", "bfs"))
        elif tool_name == "shortest_path":
            return self.get_path(arguments.get("source", ""), arguments.get("target", ""))
        elif tool_name == "get_node" or tool_name == "explain":
            return self.explain(arguments.get("label") or arguments.get("node_label", ""))
        elif tool_name == "god_nodes":
            return self.get_god_nodes()
        else:
            return f"Unknown graph tool: {tool_name}"
