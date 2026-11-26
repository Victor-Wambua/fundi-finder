"""Fundi Finder: a trust-first marketplace for local artisans."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from .config import default_config
from .db import create_all, init_db
from .security import init_security
from .sms import make_provider
from .uploads import Storage

__version__ = "0.1.0"


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(default_config())
    if config:
        app.config.update(config)

    instance = Path(app.config["INSTANCE_DIR"])
    instance.mkdir(parents=True, exist_ok=True)
    if (
        os.environ.get("FUNDI_ENV") == "production"
        and app.config["SECRET_KEY"] == "dev-only-secret-change-me"
    ):
        raise RuntimeError("Set FUNDI_SECRET_KEY before running in production.")

    init_db(app)
    create_all(app)
    app.extensions["storage"] = Storage(instance / "uploads")
    app.extensions["sms"] = make_provider(app.config)
    init_security(app)

    from . import web
    from .blueprints import admin, auth, customer, fundi, public

    app.context_processor(web.template_context)
    app.jinja_env.filters["kes"] = web.kes
    app.jinja_env.globals.update(
        kes=web.kes,
        response_label=web.response_label,
        fmt_dt=web.fmt_dt,
        fmt_date=web.fmt_date,
        fmt_dt_input=web.fmt_dt_input,
        reviewer_label=web.reviewer_label,
    )
    for bp in (public.bp, auth.bp, customer.bp, fundi.bp, admin.bp):
        app.register_blueprint(bp)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException) -> tuple[str, int]:
        code = exc.code or 500
        titles = {
            400: "That did not work",
            403: "Not allowed",
            404: "Page not found",
            413: "That file is too big",
        }
        return (
            render_template(
                "error.html",
                code=code,
                title=titles.get(code, "Something went wrong"),
                message=exc.description if code in (400, 413) else "",
            ),
            code,
        )

    from . import cli

    cli.register(app)
    logging.getLogger("fundifinder").setLevel(logging.INFO)
    return app
