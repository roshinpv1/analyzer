from unittest.mock import Mock

from codebase_agent.agents.manager import AgentManager


def _manager():
    cfg = Mock()
    cfg.get_config_value.return_value = "sqlite"
    cfg.get_max_specialist_reviews.return_value = 3
    cfg.get_skip_graphify_index.return_value = True
    cfg.get_first_review_min_confidence.return_value = 0.85
    return AgentManager(cfg)


def test_sanitize_user_output_removes_protocol_lines():
    manager = _manager()
    raw = "Good line\n<|channel|>commentary to=repo_browser.list_directory\nAnother line"
    out = manager._sanitize_user_output(raw)
    assert "repo_browser" not in out
    assert "Good line" in out
    assert "Another line" in out


def test_quality_gate_fails_on_zero_work_and_low_confidence():
    manager = _manager()
    response = "Some report\nEVIDENCE CHECK WARNING:\n..."
    stats = {
        "final_confidence": 0.2,
        "analyzer_metrics": {
            "total_actions": 0,
            "touched_file_count": 0,
            "status": "insufficient_evidence",
        },
    }
    ok, reasons = manager._passes_quality_gate(response, stats)
    assert ok is False
    assert any("0 actions" in r for r in reasons)
    assert any("confidence" in r.lower() for r in reasons)
    assert any("Evidence citation checks failed." == r for r in reasons)

