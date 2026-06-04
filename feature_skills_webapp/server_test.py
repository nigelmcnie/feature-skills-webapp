from unittest.mock import MagicMock, patch

from feature_skills_webapp.config import DEFAULT_PORT


def test_main_binds_loopback():
    mock_run = MagicMock()
    with patch("uvicorn.run", mock_run):
        from feature_skills_webapp.server import main

        main()

    assert mock_run.call_count == 1
    _, kwargs = mock_run.call_args
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == DEFAULT_PORT


def test_main_uses_configured_port(monkeypatch):
    monkeypatch.setenv("FEATURE_SKILLS_WEBAPP_PORT", "9090")
    mock_run = MagicMock()
    with patch("uvicorn.run", mock_run):
        from feature_skills_webapp.server import main

        main()

    _, kwargs = mock_run.call_args
    assert kwargs["port"] == 9090
