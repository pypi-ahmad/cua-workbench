"""Lock-in for accessibility input validation (F-041).

Cross-platform: only the validator function is exercised here. The
PowerShell / JXA scripts themselves are not run on the test host.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def helpers():
    from backend.engines import accessibility_engine as ae

    return ae


def test_validator_rejects_newline(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo\nbar", "name")


def test_validator_rejects_carriage_return(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo\rbar", "name")


def test_validator_rejects_null_byte(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo\x00bar", "name")


def test_validator_rejects_backtick(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo`whoami`", "name")


def test_validator_rejects_dollar_paren(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo$(whoami)", "name")


def test_validator_rejects_dollar_brace(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo${IFS}bar", "name")


def test_validator_rejects_semicolon(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo;rm -rf /", "name")


def test_validator_rejects_supplementary_plane(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._validate_user_string("foo\U0001F600bar", "name")


def test_validator_accepts_normal_string(helpers) -> None:
    assert helpers._validate_user_string("normal title 123", "name") == "normal title 123"
    assert helpers._validate_user_string("with\ttab", "name") == "with\ttab"
    assert helpers._validate_user_string("non-ascii ñ é", "name") == "non-ascii ñ é"


def test_ps_str_round_trips(helpers) -> None:
    """The PS expression should be a syntactically-valid PowerShell call."""
    expr = helpers._ps_str("hello world", "x")
    assert "FromBase64String" in expr
    assert "GetString" in expr
    # No raw single quotes around the user-controlled value
    assert "'hello world'" not in expr


def test_jxa_str_returns_json_literal(helpers) -> None:
    expr = helpers._jxa_str("hello", "x")
    # JSON-encoded string, ascii-safe, double-quoted
    assert expr == '"hello"'


def test_jxa_str_rejects_unsafe(helpers) -> None:
    with pytest.raises(ValueError):
        helpers._jxa_str("a;b", "x")
