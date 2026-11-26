from __future__ import annotations

from typing import Any

from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from factories import PASSWORD


def csrf(client: FlaskClient) -> str:
    with client.session_transaction() as s:
        token = s.get("csrf")
    if not token:
        client.get("/trust")
        with client.session_transaction() as s:
            token = s.get("csrf")
    return str(token)


def post(client: FlaskClient, url: str, data: dict[str, Any] | None = None, **kw: Any) -> TestResponse:
    payload = dict(data or {})
    payload.setdefault("csrf_token", csrf(client))
    return client.post(url, data=payload, **kw)


def login(client: FlaskClient, email: str, password: str = PASSWORD) -> TestResponse:
    return post(client, "/login", {"email": email, "password": password})
