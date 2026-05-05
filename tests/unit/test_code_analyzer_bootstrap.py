from unittest.mock import Mock

from codebase_agent.agents.code_analyzer import CodeAnalyzer


def _analyzer():
    tool = Mock()
    analyzer = CodeAnalyzer(config=Mock(), file_system_tool=tool)
    return analyzer


def test_inject_bootstrap_when_no_operations():
    analyzer = _analyzer()
    llm_decision = {
        "need_file_operations": False,
        "file_operations": [],
        "current_analysis": "No clear path.",
    }
    out = analyzer._inject_bootstrap_exploration_if_needed(
        llm_decision, "document auth flow", current_iteration=1, total_file_operations_executed=0
    )
    assert out["need_file_operations"] is True
    assert len(out["file_operations"]) >= 2
    assert any(op["action"] == "list_directory" for op in out["file_operations"])


def test_strip_internal_protocol_markers():
    analyzer = _analyzer()
    text = "A\n<|channel|>commentary to=repo_browser.list_directory\nB"
    cleaned = analyzer._strip_internal_protocol_markers(text)
    assert "repo_browser" not in cleaned
    assert "A" in cleaned and "B" in cleaned

