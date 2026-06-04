# feature-skills-webapp

Companion webapp for [feature-skills](https://github.com/nigelmcnie/feature-skills) (requires ≥ v2.1).

Serves the feature-skills web UI on `127.0.0.1:8800` with a SQLite-backed
persistence layer.

## Install

```bash
uv tool install git+https://github.com/nigelmcnie/feature-skills-webapp
```

## Run (manual)

```bash
feature-skills-webapp
```

Optionally override the port or DB path via environment variables:

```
FEATURE_SKILLS_WEBAPP_PORT=8800
FEATURE_SKILLS_WEBAPP_DB=~/.local/share/feature-skills-webapp/db.sqlite
```

## Run (systemd, recommended)

```bash
# Link the unit into the user systemd directory
ln -s "$(pwd)/systemd/feature-skills-webapp.service" \
      ~/.config/systemd/user/feature-skills-webapp.service

# Reload and start
systemctl --user daemon-reload
systemctl --user enable --now feature-skills-webapp

# Check it's running
systemctl --user status feature-skills-webapp
curl -s 127.0.0.1:8800/healthz
```

To view logs:

```bash
journalctl --user -u feature-skills-webapp -f
```

To stop:

```bash
systemctl --user stop feature-skills-webapp
```

## Development

```bash
uv sync
uv run pytest
```
