# webapp-skeleton

## Problem space and motivation

The webapp project is starting from an empty repository. Before any real feature can be built — inbox, doc discovery, read state — there needs to be a running process, a place to put data, and a way to keep it running across reboots. `webapp-skeleton` is that foundation.

Every other feature in the webapp depends on this one. Getting the bones in first lets subsequent work build on a real server with a real schema rather than bootstrapping their own environments.

## Related work

The kea project uses the same Starlette + Jinja2 + numbered-SQL-migration pattern. Nigel is fluent in it and the design doc explicitly calls it out as the reference: *"Use the same Starlette + Jinja2 + numbered-SQL-migration pattern kea uses."*

The full data model is specified in §4 of the webapp design doc and should be reflected in the initial schema migration, even though most tables will be empty until later features populate them.

## Constraints and considerations

- **Listen on `127.0.0.1` only.** No auth, no remote access. The design principle is local-only by default.
- **Port 8800** is the suggested default; it should be configurable (env var or config). Other features will hardcode `localhost:8800` for health checks and webapp detection.
- **systemd user unit** at `~/.config/systemd/user/feature-skills-webapp.service` — starts on login, restarts on crash. This is the supervision mechanism the design doc specifies; the autostart alternative was noted but systemd preferred for robustness.
- **Python 3.14.** The design doc specifies this; use whatever mise/pyenv provides in the current environment rather than pinning tightly in a way that breaks on minor bumps.
- **Schema migrations must be numbered** (e.g. `0001_init.sql`) so future features can add columns without squashing history.
- **SQLite location is still an open question** (see below). Pick a reasonable default for the skeleton and make it configurable.

## Links

- Design doc: [feature-skills-webapp design doc](file:///home/nigel/src/nigelmcnie/feature-skills/docs/webapp.html) — §3 Architecture, §4 Data model, §6 webapp-skeleton feature card

## Open questions

1. **SQLite location:** `~/.claude/feature-docs/_webapp.db` (keeps everything together, chezmoi-friendly) vs `~/.local/share/feature-skills-webapp/db.sqlite` (follows XDG). The design doc leaves this open. Pick one for the skeleton — it's easy to move later since Stage 1 data is all derived from the filesystem.
2. **Port configuration mechanism:** environment variable (`FEATURE_SKILLS_WEBAPP_PORT`), a `~/.config/feature-skills-webapp/config.toml`, or a CLI arg passed from the systemd unit? Env var in the service file is the simplest.
3. **HTMX version:** bundle it or CDN? Since this is local-only and offline use is plausible, bundling is safer.
