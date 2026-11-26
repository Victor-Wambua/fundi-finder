from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy.orm import Session

from fundifinder import clock, create_app

NOW = datetime(2026, 6, 15, 10, 0, 0)
SECRET = "test-secret"


@pytest.fixture(autouse=True)
def frozen_clock() -> Iterator[None]:
    clock.set_frozen(NOW)
    yield
    clock.set_frozen(None)


def _config(tmp: Path) -> dict[str, object]:
    return {
        "TESTING": True,
        "SECRET_KEY": SECRET,
        "INSTANCE_DIR": str(tmp),
        "DATABASE_URL": f"sqlite:///{tmp / 'test.db'}",
        "PASSWORD_HASH_METHOD": "pbkdf2:sha256:1000",
        "RATELIMIT_ENABLED": False,
    }


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    return create_app(_config(tmp_path))


@pytest.fixture
def db(app: Flask) -> Iterator[Session]:
    """A standalone session (not the request-scoped one), like a second connection."""
    session = app.extensions["sessionmaker"]()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


@pytest.fixture(scope="session")
def seeded_app(tmp_path_factory: pytest.TempPathFactory) -> Flask:
    from fundifinder.seed import seed_demo

    clock.set_frozen(NOW)
    try:
        app = create_app(_config(tmp_path_factory.mktemp("seeded")))
        with app.app_context():
            seed_demo(app)
    finally:
        clock.set_frozen(None)
    return app


@pytest.fixture
def seeded_db(seeded_app: Flask) -> Iterator[Session]:
    session = seeded_app.extensions["sessionmaker"]()
    yield session
    session.rollback()
    session.close()
