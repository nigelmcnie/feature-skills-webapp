import os
from pathlib import Path

DEFAULT_PORT = 8800


class ConfigError(Exception):
    pass


def db_path() -> Path:
    override = os.environ.get("FEATURE_SKILLS_WEBAPP_DB")
    if override:
        return Path(override)
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return xdg / "feature-skills-webapp" / "db.sqlite"


def port() -> int:
    raw = os.environ.get("FEATURE_SKILLS_WEBAPP_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"FEATURE_SKILLS_WEBAPP_PORT must be an integer, got {raw!r}") from e
    if not (1 <= value <= 65535):
        raise ConfigError(f"FEATURE_SKILLS_WEBAPP_PORT out of range: {value}")
    return value
