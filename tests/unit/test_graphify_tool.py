"""GraphifyTool in-process graph dispatch."""

import json

import pytest

from codebase_agent.tools.graphify_tool import GraphifyTool


@pytest.fixture
def tiny_codebase(tmp_path):
    out = tmp_path / "graphify-out"
    out.mkdir(parents=True)
    data = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "svc_auth",
                "label": "AuthService",
                "source_file": "app/auth.py",
                "file_type": "code",
                "community": 0,
            },
            {
                "id": "mdl_user",
                "label": "UserModel",
                "source_file": "app/user.py",
                "file_type": "code",
                "community": 0,
            },
        ],
        "links": [
            {
                "source": "svc_auth",
                "target": "mdl_user",
                "relation": "references",
                "confidence": "EXTRACTED",
            }
        ],
    }
    (out / "graph.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_graphify_tool_query_matches_labels(tiny_codebase):
    tool = GraphifyTool(str(tiny_codebase))
    text = tool.execute_tool(
        "query_graph",
        {"question": "authentication user model", "mode": "bfs", "depth": 2},
    )
    assert "AuthService" in text or "UserModel" in text


def test_graphify_tool_god_nodes_not_bogus_query(tiny_codebase):
    tool = GraphifyTool(str(tiny_codebase))
    text = tool.execute_tool("god_nodes", {"top_n": 5})
    assert "God nodes" in text
    assert "AuthService" in text or "UserModel" in text


def test_graphify_tool_missing_graph(tmp_path):
    tool = GraphifyTool(str(tmp_path))
    err = tool.execute_tool("query_graph", {"question": "anything"})
    assert "No graph index" in err or "Graph file not found" in err


def test_graphify_tool_query_fallback_when_no_lexical_match(tiny_codebase):
    """No token overlap with labels still returns hub neighborhood (not empty)."""
    tool = GraphifyTool(str(tiny_codebase))
    text = tool.execute_tool(
        "query_graph",
        {"question": "qqqzzz unrelated tokens", "mode": "bfs", "depth": 3},
    )
    assert "No keyword overlap" in text or "hub" in text.lower()
    assert "AuthService" in text or "UserModel" in text
