"""Local web session signing and CSRF helpers.

There is intentionally no multi-user authentication: the admin UI binds to a
loopback address only. A signed, HttpOnly cookie carries a random CSRF token;
every state-changing form must echo it. The signing secret lives in
``runtime/web-secret`` and never enters configuration, logs or diagnostics.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE_NAME = "cater_session"
CSRF_FIELD = "csrf_token"
SESSION_SALT = "cater-local-web-session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def ensure_web_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    if existing and len(existing) >= 32:
        return existing
    value = secrets.token_hex(32)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="ascii")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    return value


def request_host_allowed(host_header: str | None) -> bool:
    """Reject DNS-rebinding style Host headers even though we bind loopback."""

    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return False
        host = host[: closing + 1]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


def _split_origin(value: str | None) -> tuple[str, str, int] | None:
    """Normalize an origin into ``(scheme, host, port)`` or ``None``.

    Only loopback http(s) origins without userinfo or path components are
    accepted. Default ports are normalized so ``http://localhost`` equals
    ``http://localhost:80``.
    """

    if not value:
        return None
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    host = parsed.hostname.lower().strip("[]")
    if host not in {item.strip("[]") for item in LOOPBACK_HOSTS}:
        return None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return (parsed.scheme, host, port)


def origin_allowed(origin: str | None, request_origin: str | None) -> bool:
    """Exact same-origin check for browser form posts.

    * No ``Origin`` header (older clients, CLI, TestClient): allowed; the
      CSRF token validation still applies.
    * ``Origin: null`` or an unparsable/hostile origin: rejected.
    * Otherwise scheme, host and port must match the current request origin
      exactly. ``localhost``, ``127.0.0.1`` and ``::1`` each only match
      themselves; a different loopback name, port or scheme is cross-origin.
    """

    if not origin:
        return True
    if origin.strip().lower() == "null":
        return False
    parsed = _split_origin(origin)
    expected = _split_origin(request_origin)
    if parsed is None or expected is None:
        return False
    return parsed == expected


class SessionSigner:
    def __init__(self, secret: str, *, max_age_seconds: int = 8 * 60 * 60) -> None:
        if len(secret) < 32:
            raise ValueError("web session secret is too short")
        self.max_age_seconds = max(60, int(max_age_seconds))
        self._serializer = URLSafeTimedSerializer(
            secret,
            salt=SESSION_SALT,
            signer_kwargs={"digest_method": hashlib.sha256},
        )

    def load(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = self._serializer.loads(value, max_age=self.max_age_seconds)
        except (BadSignature, SignatureExpired):
            return {}
        return data if isinstance(data, dict) else {}

    def dump(self, session: dict[str, Any]) -> str:
        return self._serializer.dumps(session)

    @staticmethod
    def new_session() -> dict[str, Any]:
        return {"csrf": secrets.token_urlsafe(32)}

    @staticmethod
    def tokens_equal(expected: str | None, supplied: str | None) -> bool:
        if not expected or not supplied:
            return False
        return hmac.compare_digest(str(expected), str(supplied))