"""Bearer-token gate in front of the HTTP app.

The token is read from a file (a compose secret), never from the environment: an env var is visible to
anyone who can run `docker inspect`. The server refuses to start without a token unless auth is switched
off explicitly, so a missing secret cannot silently leave the tailnet port open.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path

TOKEN_FILE = os.environ.get("KNOWLEDGE_TOKEN_FILE", "/run/secrets/knowledge_token")
MIN_LENGTH = 32  # `openssl rand -hex 32` gives 64; anything shorter is a typo or a placeholder


def load_token(path: str | Path = TOKEN_FILE) -> str:
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SystemExit(f"auth: token file {path} is unreadable ({type(e).__name__}); "
                         "mount the secret or set KNOWLEDGE_AUTH=off") from e
    if len(token) < MIN_LENGTH:
        raise SystemExit(f"auth: token in {path} is shorter than {MIN_LENGTH} characters")
    return token


class BearerAuth:
    """ASGI middleware: every HTTP request must carry `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        self.app, self._token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan and websocket events pass through untouched
            await self.app(scope, receive, send)
            return
        header = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, given = header.partition(b" ")
        if scheme.lower() == b"bearer" and hmac.compare_digest(given.strip(), self._token):
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized"}'
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", b"Bearer")]})
        await send({"type": "http.response.body", "body": body})
