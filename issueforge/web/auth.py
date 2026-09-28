import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from issueforge.config import settings

logger = logging.getLogger("issueforge.web.auth")

# Webhook routes verify a platform signature of their own (GitHubClient.verify_webhook_signature,
# GitLabClient.verify_webhook_token) -- but both verifiers return True unconditionally when no
# webhook secret is configured. Exempting a webhook route outright would then leave it wide open
# on an exposed instance, so each entry here is only exempt while its secret is actually set;
# otherwise the route is gated like any other (an operator can put ?token= in the webhook URL).
WEBHOOK_EXEMPTIONS = {
    "/api/webhooks/github": "github_webhook_secret",
    "/api/webhooks/gitlab": "gitlab_webhook_secret",
}


def token_is_valid(supplied: Optional[str]) -> bool:
    """Constant-time check of a supplied token against the configured one.

    True when no token is configured at all. `issueforge start` always configures one (see
    load_or_create_token); only a bare create_app(), as in tests, runs ungated.
    Shared by the HTTP middleware and both WebSocket handlers so the compare lives in
    exactly one place.
    """
    expected = settings.forge_auth_token
    if not expected:
        return True
    # hmac.compare_digest only accepts ASCII-only str; a non-ASCII token (or a supplied
    # value from URL-decoding, e.g. ?token=%C3%A9) would otherwise raise TypeError, which
    # surfaces as an unhandled 500 instead of a clean 401 -- comparing bytes avoids that.
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def load_or_create_token(path: Path) -> str:
    """Return the dashboard token stored at `path`, creating it (0o600) on first use."""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise SystemExit(f"{path} is empty. Delete it to generate a new token.")
        return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def websocket_is_allowed(websocket: WebSocket) -> bool:
    """Gate for both WebSocket handlers; call before accept().

    Browsers apply no CORS to WebSockets, so any page the operator visits could otherwise
    open one. A present Origin must match the Host the client connected to. A missing Origin
    means a non-browser client and is allowed; the token check still applies.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc.lower() != websocket.headers.get("host", "").lower():
        return False
    supplied = websocket.query_params.get("token") or websocket.cookies.get("forge_token")
    return token_is_valid(supplied)


def _is_exempt_webhook(path: str) -> bool:
    secret_attr = WEBHOOK_EXEMPTIONS.get(path)
    return bool(secret_attr and getattr(settings, secret_attr, None))


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require a shared token on every request when one is configured.

    Pass-through when no token is set (only a bare create_app(); `start` always sets one).
    When a token is set, every route is gated -- including a webhook route, unless its
    platform secret is configured (see WEBHOOK_EXEMPTIONS above).
    """

    async def dispatch(self, request: Request, call_next):
        if not settings.forge_auth_token or _is_exempt_webhook(request.url.path):
            return await call_next(request)

        supplied = self._supplied_token(request)
        if not token_is_valid(supplied):
            return JSONResponse({"detail": "Unauthorized."}, status_code=401)

        response = await call_next(request)
        if request.query_params.get("token"):
            # Let the browser stop carrying the token in every URL.
            response.set_cookie(
                "forge_token", settings.forge_auth_token, httponly=True, samesite="strict"
            )
        return response

    @staticmethod
    def _supplied_token(request: Request) -> Optional[str]:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.query_params.get("token") or request.cookies.get("forge_token")
