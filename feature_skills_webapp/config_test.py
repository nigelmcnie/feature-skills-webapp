import os

import pytest

from feature_skills_webapp.config import DEFAULT_PORT, ConfigError, db_path, docs_root, port


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


def test_docs_root_default(monkeypatch):
    monkeypatch.delenv("FEATURE_SKILLS_WEBAPP_DOCS_ROOT", raising=False)
    result = docs_root()
    from pathlib import Path

    assert result == Path.home() / ".claude" / "feature-docs"


def test_docs_root_valid_override(monkeypatch, tmp_path):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_DOCS_ROOT", str(tmp_path))
    assert docs_root() == tmp_path


def test_docs_root_non_dir_override(monkeypatch, tmp_path):
    non_dir = str(tmp_path / "does-not-exist")
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_DOCS_ROOT", non_dir)
    with pytest.raises(ConfigError, match="is not a directory"):
        docs_root()


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
