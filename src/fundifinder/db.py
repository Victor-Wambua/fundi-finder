"""SQLAlchemy engine/session wiring (plain SQLAlchemy 2, one session per request)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

if TYPE_CHECKING:
    from flask import Flask

# Flask is imported inside the functions that need it, so the models and the domain modules
# can be imported (and run, for example in the browser demo) without the web framework.


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    engine = create_engine(url)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

    return engine


def init_db(app: Flask) -> None:
    from flask import g

    engine = make_engine(app.config["DATABASE_URL"])
    app.extensions["engine"] = engine
    app.extensions["sessionmaker"] = sessionmaker(engine, expire_on_commit=False)

    @app.teardown_appcontext
    def _close(_exc: BaseException | None) -> None:
        session = g.pop("db", None)
        if session is not None:
            session.close()


def get_db() -> Session:
    from flask import current_app, g

    if "db" not in g:
        g.db = current_app.extensions["sessionmaker"]()
    session: Session = g.db
    return session


def create_all(app: Flask) -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(app.extensions["engine"])
