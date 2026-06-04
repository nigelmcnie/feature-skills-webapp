import os
import tempfile
from pathlib import Path

_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
os.environ.setdefault(
    "FEATURE_SKILLS_WEBAPP_DB",
    str(Path(tempfile.gettempdir()) / f"fsw-test-{_worker}.db"),
)
