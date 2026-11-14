from __future__ import annotations

from mcp_obsidian.limits import env_float, env_int


def test_invalid_integer_environment_value_uses_default(monkeypatch):
    monkeypatch.setenv("TEST_INTEGER", "not-an-integer")

    assert env_int("TEST_INTEGER", 10, 1, 20) == 10


def test_integer_environment_value_is_clamped(monkeypatch):
    monkeypatch.setenv("TEST_INTEGER", "0")
    assert env_int("TEST_INTEGER", 10, 1, 20) == 1

    monkeypatch.setenv("TEST_INTEGER", "100")
    assert env_int("TEST_INTEGER", 10, 1, 20) == 20


def test_non_finite_float_environment_value_uses_default(monkeypatch):
    for value in ("not-a-float", "nan", "inf", "-inf"):
        monkeypatch.setenv("TEST_FLOAT", value)
        assert env_float("TEST_FLOAT", 10.0, 0.1, 20.0) == 10.0


def test_float_environment_value_is_clamped(monkeypatch):
    monkeypatch.setenv("TEST_FLOAT", "0")
    assert env_float("TEST_FLOAT", 10.0, 0.1, 20.0) == 0.1

    monkeypatch.setenv("TEST_FLOAT", "100")
    assert env_float("TEST_FLOAT", 10.0, 0.1, 20.0) == 20.0
