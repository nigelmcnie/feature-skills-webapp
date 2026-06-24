# feature-skills-webapp

Self-hosted Starlette + SQLite webapp companion to feature-skills. Python, managed with `uv`.

## QA / quality control

Run all of these before committing; all must pass:

```bash
uv run ruff format .      # or: uv run ruff format --check .  (CI)
uv run ruff check .
uv run ty check .
uv run pytest             # xdist + pytest-socket; per-worker DB
```

## Running the deployed service

The systemd user service runs the `uv tool`-installed entrypoint
(`~/.local/bin/feature-skills-webapp`), whose environment is separate from the
project `.venv` that tests and `uv run` use. So the long-running service won't
reflect your edits until you act:

- **Code changes**: restart it — `systemctl --user restart feature-skills-webapp`.
- **Dependency changes** (anything in `pyproject.toml`): reinstall *and* restart,
  or the service crash-loops on `ModuleNotFoundError` —
  `uv tool install --editable . --reinstall && systemctl --user restart feature-skills-webapp`.

**Never run `uv tool install` from a worktree.** The install pins the editable
source to whichever directory it ran from. If that directory is a worktree, the
service breaks (500 on every page) when the worktree is removed. Always run
from the main checkout. To confirm: `cat ~/.local/share/uv/tools/feature-skills-webapp/uv-receipt.toml`.
