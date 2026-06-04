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

By default the server listens on `127.0.0.1:8800` and stores its SQLite
database at `~/.local/share/feature-skills-webapp/db.sqlite` (XDG data dir).
The systemd unit sets only the port; the DB path falls back to this default.
Override either via environment variables:

```
FEATURE_SKILLS_WEBAPP_PORT=8800
FEATURE_SKILLS_WEBAPP_DB=~/.local/share/feature-skills-webapp/db.sqlite
```

## Run (systemd, recommended)

### Install the unit

```bash
# Clone or pull this repo, then symlink the unit:
ln -s "$(pwd)/systemd/feature-skills-webapp.service" \
      ~/.config/systemd/user/feature-skills-webapp.service

systemctl --user daemon-reload
systemctl --user enable --now feature-skills-webapp
```

### Verify it's running

```bash
systemctl --user status feature-skills-webapp
curl -s 127.0.0.1:8800/healthz
# Expected: {"status":"ok"}
```

### Logs

```bash
journalctl --user -u feature-skills-webapp -f
```

### Stop / start

```bash
systemctl --user stop feature-skills-webapp
systemctl --user start feature-skills-webapp
```

### Verify restart-on-crash

```bash
# Find the worker PID and kill it — systemd should restart it within 2s.
kill $(systemctl --user show -p MainPID --value feature-skills-webapp)
sleep 3
systemctl --user status feature-skills-webapp   # should show active (running)
```

### Verify clean shutdown

```bash
systemctl --user stop feature-skills-webapp
# Uvicorn drains in-flight requests and exits cleanly.
# No long-lived DB handle is held (connections are per-request),
# so WAL sidecar files (.db-wal, .db-shm) persist harmlessly and
# checkpoint on next open.
journalctl --user -u feature-skills-webapp -n 10
# Expected: "Finished server process" with no error lines
```

### Verify port-conflict behaviour

```bash
# Occupy 8800 with another process, then try to start:
python3 -m http.server 8800 &
systemctl --user start feature-skills-webapp
sleep 65   # wait for start-limit (5 attempts × 2s backoff within 60s window)
systemctl --user status feature-skills-webapp
# Expected: failed state, "start request repeated too quickly" in journal
kill %1
```

### Survive logout / login

The `WantedBy=default.target` + `enable` ensure the service starts automatically
when the user session comes up. Verify after re-login:

```bash
curl -s 127.0.0.1:8800/healthz
# Expected: {"status":"ok"}
```

## Development

```bash
uv sync
uv run pytest
uv run ruff format . && uv run ruff check . && uv run ty check .
```
