import os

import pytest

from feature_skills_webapp.config import (
    DEFAULT_PORT,
    DEFAULT_WAIT_TIMEOUT,
    ConfigError,
    db_path,
    port,
    wait_timeout,
)


def test_wait_timeout_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT", raising=False)
    assert wait_timeout() == DEFAULT_WAIT_TIMEOUT


def test_wait_timeout_default_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT", "")
    assert wait_timeout() == DEFAULT_WAIT_TIMEOUT


def test_wait_timeout_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT", "5.0")
    assert wait_timeout() == 5.0


def test_wait_timeout_non_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_WAIT_TIMEOUT", "abc")
    with pytest.raises(ConfigError, match="must be a float"):
        wait_timeout()


def test_port_default_when_unset(monkeypatch):
    monkeypatch.delenv("FEATURE_SKILLS_WEBAPP_PORT", raising=False)
    assert port() == DEFAULT_PORT


def test_port_default_when_empty(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PORT", "")
    assert port() == DEFAULT_PORT


def test_port_valid(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PORT", "9000")
    assert port() == 9000


def test_port_non_integer(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PORT", "abc")
    with pytest.raises(ConfigError, match="must be an integer"):
        port()


def test_port_zero(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PORT", "0")
    with pytest.raises(ConfigError, match="out of range"):
        port()


def test_port_too_large(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PORT", "99999")
    with pytest.raises(ConfigError, match="out of range"):
        port()


def test_db_path_env_override(monkeypatch, tmp_path):
    target = str(tmp_path / "custom.db")
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_DB", target)
    assert str(db_path()) == target


def test_db_path_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("FEATURE_SKILLS_WEBAPP_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = db_path()
    assert result == tmp_path / "feature-skills-webapp" / "db.sqlite"


def test_db_path_home_fallback(monkeypatch):
    monkeypatch.delenv("FEATURE_SKILLS_WEBAPP_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    result = db_path()
    home = os.path.expanduser("~")
    assert str(result).startswith(home)
    assert "feature-skills-webapp" in str(result)
    assert result.name == "db.sqlite"
