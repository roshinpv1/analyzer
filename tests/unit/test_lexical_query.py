"""Tests for graphify lexical question tokenization."""

from codebase_agent.graphify.lexical_query import terms_from_question


def test_terms_splits_whitespace():
    assert terms_from_question("auth service login") == ["auth", "service", "login"]


def test_terms_camel_case():
    t = terms_from_question("OAuthFlowHandler")
    assert "flow" in t and "handler" in t
    # Leading acronym + word boundaries vary; at least one auth-related token appears.
    assert "auth" in t or "oauth" in t


def test_terms_pascal_acronym_boundary():
    t = terms_from_question("parseXMLDocument")
    assert "parse" in t
    assert "xml" in t
    assert "document" in t


def test_terms_dedupes():
    t = terms_from_question("auth auth token")
    assert t.count("auth") == 1


def test_empty_question():
    assert terms_from_question("") == []
    assert terms_from_question("   ") == []
