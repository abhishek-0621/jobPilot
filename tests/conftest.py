from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from jobpilot.data.db import make_engine, make_session_factory, upgrade_to_head


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    db_path = tmp_path / "t.db"
    upgrade_to_head(db_path)
    engine = make_engine(db_path)
    session = make_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
