"""
Auth0 login routes (see app/auth0.py). Nothing here touches /auth/*: the
existing register/login/me endpoints and their tokens are unchanged, and a
user who signs in through Auth0 ends up holding the same kind of access
token /auth/login hands out.

The login round-trip's state/nonce live in a short-lived, HS256-signed,
HttpOnly cookie -- no server-side session store is needed,
and no middleware is added to the rest of the app.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import auth0 as auth0_service
from app.auth import ALGORITHM, SECRET_KEY, create_access_token
from app.database import get_db

logger = logging.getLogger(__name__)

_CALLBACK_PATHS = ("/auth/callback", "/auth0/callback")
_REDACTED_PARAMS = ("code", "state")


class RedactCallbackQuery(logging.Filter):
    """
    Uvicorn's access log records every request line, and Auth0 redirects to
    the callback with ?code=...&state=... -- a one-time authorization code
    that must not sit in log files. This blanks those two values on the
    callback paths only; every other access-log line is untouched.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, query = args[2].partition("?")
            if sep and path.startswith(_CALLBACK_PATHS):
                parts = []
                for pair in query.split("&"):
                    key = pair.split("=", 1)[0]
                    parts.append(f"{key}=[redacted]" if key in _REDACTED_PARAMS else pair)
                record.args = args[:2] + (f"{path}?{'&'.join(parts)}",) + args[3:]
        return True


logging.getLogger("uvicorn.access").addFilter(RedactCallbackQuery())

router = APIRouter(prefix="/auth0", tags=["auth0"])
# Separate, prefix-less router for /auth/callback/ -- see auth_callback below.
callback_router = APIRouter(tags=["auth0"])

STATE_COOKIE = "auth0_login_state"
STATE_COOKIE_MAX_AGE_SECONDS = 600
# "/" rather than "/auth0" so the browser also sends it to /auth/callback/.
STATE_COOKIE_PATH = "/"
# Auth0 codes/states are well under this; anything longer is junk.
MAX_CALLBACK_PARAM_LENGTH = 2048


_PROVIDER_NAMES = {"google": "Google", "facebook": "Facebook"}


def _require_configured() -> None:
    problems = auth0_service.config_problems()
    if problems:
        # Variable names only -- config_problems never includes values.
        logger.error("Auth0 login unavailable, configuration problem(s): %s", "; ".join(problems))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Auth0 login is not configured")


def _redirect_with_fragment(fragment: dict) -> RedirectResponse:
    # The token travels in the URL fragment, which browsers never send to
    # any server, so it doesn't end up in access logs or Referer headers.
    response = RedirectResponse(
        f"{auth0_service.AUTH0_POST_LOGIN_REDIRECT}#{urlencode(fragment)}",
        status_code=status.HTTP_302_FOUND,
    )
    response.delete_cookie(STATE_COOKIE, path=STATE_COOKIE_PATH)
    return response


@router.get("/status")
def auth0_status():
    """Lets the dashboard decide whether to show its "Continue with Auth0" button."""
    return {"enabled": auth0_service.is_configured()}


@router.get("/login")
def auth0_login(provider: str | None = None):
    """
    Starts an Auth0 login. With no `provider`, Auth0 shows its own login
    page; with `provider=google` or `provider=facebook` it goes straight
    to that provider (see auth0_service.SOCIAL_CONNECTIONS). Either way the browser comes back
    through the same callback.
    """
    _require_configured()
    connection = None
    if provider is not None:
        connection = auth0_service.SOCIAL_CONNECTIONS.get(provider)
        if connection is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported login provider")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    try:
        signed_state = jwt.encode(
            {
                "state": state,
                "nonce": nonce,
                # Only used to word error messages ("Facebook sign-in was cancelled").
                "provider": provider or "auth0",
                "exp": datetime.now(timezone.utc) + timedelta(seconds=STATE_COOKIE_MAX_AGE_SECONDS),
            },
            SECRET_KEY,
            algorithm=ALGORITHM,
        )
        authorize_url = auth0_service.build_authorize_url(
            state, nonce, connection, auth0_service.SOCIAL_EXTRA_PARAMS.get(provider)
        )
    except Exception:
        # e.g. a broken SECRET_KEY/ALGORITHM. Stack trace to the log only.
        logger.exception("Auth0 login could not start (provider=%s)", provider or "auth0")
        return _redirect_with_fragment({"auth0_error": auth0_service.MSG_SERVER})
    response = RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        STATE_COOKIE,
        signed_state,
        max_age=STATE_COOKIE_MAX_AGE_SECONDS,
        path=STATE_COOKIE_PATH,
        httponly=True,
        # Lax still sends the cookie on Auth0's top-level GET redirect back.
        samesite="lax",
        secure=auth0_service.AUTH0_CALLBACK_URL.startswith("https://"),
    )
    return response


def _friendly_error(error: str, error_description: str | None, provider: str | None = None) -> str:
    """
    Turns the Auth0 errors a user realistically hits into plain wording,
    naming Google/Facebook when the login was started for one of them.
    The raw values are still logged by _reject.
    """
    name = _PROVIDER_NAMES.get(provider or "")
    description = (error_description or "").lower()
    if "connection is not enabled" in description:
        # e.g. "Continue with Facebook" before Facebook is switched on in the Auth0 tenant.
        if name:
            return f"{name} sign-in isn't available right now. Please use another way to log in."
        return "That sign-in option isn't available right now. Please use another way to log in."
    if error == "access_denied":
        # Auth0's access_denied descriptions are written for end users
        # (e.g. "User cancelled"), so they're safe to show.
        return error_description or (f"{name} sign-in was cancelled." if name else "Sign-in was cancelled.")
    # Anything else (invalid_request, server_error, ...) is technical; the
    # user gets plain wording and the raw value only goes to the server log.
    if name:
        return f"{name} sign-in didn't complete. Please try again or use another way to log in."
    return "Sign-in didn't complete. Please try again or use another way to log in."


def _reject(reason: str, user_message: str, provider: str | None = None) -> RedirectResponse:
    # Only the reason is logged -- never the code, state, tokens or secret.
    logger.warning("Auth0 callback rejected (provider=%s): %s", provider or "unknown", reason)
    return _redirect_with_fragment({"auth0_error": user_message})


def _read_state_cookie(request: Request) -> dict | None:
    try:
        return jwt.decode(request.cookies.get(STATE_COOKIE, ""), SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def _complete_login(
    request: Request,
    code: str | None,
    state: str | None,
    error: str | None,
    error_description: str | None,
    db: Session,
) -> RedirectResponse:
    _require_configured()
    cookie_claims = _read_state_cookie(request)
    provider = (cookie_claims or {}).get("provider")

    # 1. Auth0 reported a failure itself (user cancelled, app misconfigured, ...).
    if error:
        return _reject(
            f"auth0 returned error={error!r} ({error_description!r})",
            _friendly_error(error, error_description, provider),
            provider,
        )

    # 2. Required callback parameters.
    if not code or not state:
        missing = ", ".join(name for name, value in (("code", code), ("state", state)) if not value)
        return _reject(f"missing {missing}", "Login response from Auth0 was incomplete. Please try again.", provider)
    if len(code) > MAX_CALLBACK_PARAM_LENGTH or len(state) > MAX_CALLBACK_PARAM_LENGTH:
        return _reject("oversized code/state", "Login response from Auth0 was invalid. Please try again.", provider)

    # 3. The state must match the one /auth0/login issued to this browser (CSRF protection).
    if cookie_claims is None:
        return _reject("state cookie missing, tampered or expired", "Login session expired. Please try again.")
    expected_state = str(cookie_claims.get("state", ""))
    if not expected_state or not secrets.compare_digest(state, expected_state):
        return _reject("state mismatch", "Your sign-in couldn't be matched to this browser. Please start again.", provider)

    # 4. Exchange the code server-side (client secret never leaves the server),
    #    verify the ID token, map it onto a local user, and issue the app's token.
    try:
        tokens = auth0_service.exchange_code_for_tokens(code)
        claims = auth0_service.verify_id_token(
            tokens.get("id_token", ""), str(cookie_claims.get("nonce", "")), tokens.get("access_token")
        )
        user = auth0_service.get_or_create_user(db, claims)
        # Auth0's own tokens are discarded here; the browser only ever receives
        # the app's usual access token, exactly like /auth/login returns.
        access_token = create_access_token(data={"sub": str(user.id)})
    except auth0_service.Auth0Error as exc:
        db.rollback()
        return _reject(str(exc), exc.user_message, provider)
    except SQLAlchemyError:
        # Database down, constraint or schema problem while finding/creating
        # the user. Stack trace to the server log only.
        db.rollback()
        logger.exception("Auth0 callback: database error while signing in (provider=%s)", provider or "unknown")
        return _redirect_with_fragment({"auth0_error": auth0_service.MSG_SERVER})
    except Exception:
        # Anything unexpected -- including failing to create the app's own
        # JWT -- still lands the user back on the page with a plain message
        # instead of a raw 500. Stack trace to the server log only.
        db.rollback()
        logger.exception("Auth0 callback: unexpected error while signing in (provider=%s)", provider or "unknown")
        return _redirect_with_fragment({"auth0_error": auth0_service.MSG_SERVER})

    logger.info("Auth0 sign-in succeeded (provider=%s, user_id=%s)", provider or "auth0", user.id)
    return _redirect_with_fragment({"access_token": access_token, "token_type": "bearer", "login_method": "auth0"})


@router.get("/callback")
def auth0_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    """Original callback path -- kept working so an Auth0 app still pointing here doesn't break."""
    return _complete_login(request, code, state, error, error_description, db)


@callback_router.get("/auth/callback/", summary="Auth0 Callback")
def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Auth0 redirects the browser here after login (set AUTH0_CALLBACK_URL to
    this path). Lives in its own router, not app/routers/auth.py, so the
    existing /auth/register, /auth/login and /auth/me are untouched.
    """
    return _complete_login(request, code, state, error, error_description, db)


@router.get("/logout")
def auth0_logout():
    """
    Ends the Auth0 session too; otherwise the next "Continue with Auth0"
    would sign straight back in without asking. The app's own token is
    stateless -- the dashboard already discards it client-side.
    """
    _require_configured()
    return RedirectResponse(auth0_service.build_logout_url(), status_code=status.HTTP_302_FOUND)
