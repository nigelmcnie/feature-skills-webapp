import os
import tempfile
from pathlib import Path

import pytest

_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
os.environ.setdefault(
    "FEATURE_SKILLS_WEBAPP_DB",
    str(Path(tempfile.gettempdir()) / f"fsw-test-{_worker}.db"),
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    from feature_skills_webapp.storage.db import connect, migrate

    db = tmp_path / "test.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    return db
