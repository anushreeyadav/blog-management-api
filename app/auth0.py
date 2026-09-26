"""
Auth0 login, added alongside -- never in place of -- the existing
username/password flow in app/auth.py.

The browser is sent to Auth0's Universal Login page (Authorization Code
flow). On the way back, app/routers/auth0.py exchanges the code for Auth0's
ID token, which is verified here (RS256 against the tenant's published
JWKS, plus issuer/audience/expiry/nonce), then mapped onto a local
models.User. The caller then gets the very same HS256 access token
/auth/login issues (app.auth.create_access_token), so every existing
protected route keeps working through get_current_user unchanged.

Auth0 is entirely optional: with AUTH0_DOMAIN / AUTH0_CLIENT_ID /
AUTH0_CLIENT_SECRET unset, is_configured() is False and the /auth0/*
routes answer 503 instead of redirecting anywhere.
"""

import logging
import os
import re
import secrets

import httpx
from dotenv import load_dotenv
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_password
from app.services import subscription as subscription_service

load_dotenv()

logger = logging.getLogger(__name__)

AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "").strip().removeprefix("https://").rstrip("/")
AUTH0_CLIENT_ID = os.getenv("AUTH0_CLIENT_ID", "").strip()
AUTH0_CLIENT_SECRET = os.getenv("AUTH0_CLIENT_SECRET", "").strip()
AUTH0_CALLBACK_URL = os.getenv("AUTH0_CALLBACK_URL", "http://localhost:8000/auth0/callback").strip()
# Where the browser lands after a successful Auth0 login (the new access
# token rides along in the URL fragment) and after an Auth0 logout.
AUTH0_POST_LOGIN_REDIRECT = os.getenv("AUTH0_POST_LOGIN_REDIRECT", "/static/dashboard.html").strip()
AUTH0_LOGOUT_RETURN_URL = os.getenv(
    "AUTH0_LOGOUT_RETURN_URL", "http://localhost:8000/static/dashboard.html"
).strip()

AUTH0_SCOPE = "openid profile email"
# Social logins the app may ask Auth0 to jump straight to (Auth0's
# `connection` parameter), keyed by the short name /auth0/login accepts.
# Anything not listed here is refused, so a crafted link can't pick an
# arbitrary connection. Each name is overridable in case the tenant's
# connection was created under a custom name.
SOCIAL_CONNECTIONS = {
    "google": os.getenv("AUTH0_GOOGLE_CONNECTION", "google-oauth2").strip() or "google-oauth2",
    "facebook": os.getenv("AUTH0_FACEBOOK_CONNECTION", "facebook").strip() or "facebook",
}
# Extra /authorize parameters per provider. Facebook asks for consent every
# time, so a user who earlier declined "email" is shown the permission
# screen again instead of being signed straight through.
SOCIAL_EXTRA_PARAMS = {
    "facebook": {"prompt": "consent"},
}
HTTP_TIMEOUT_SECONDS = 10.0

# The tenant's public signing keys, fetched lazily and re-fetched once
# whenever an ID token names a key id ("kid") not seen yet (key rotation).
_jwks_cache: dict | None = None


# Plain-language messages shown to users. Technical detail goes only to
# the server log (see Auth0Error below and app/routers/auth0.py).
MSG_RETRY = "We couldn't complete your sign-in. Please try again."
MSG_VERIFY = "We couldn't verify your sign-in. Please try again."
MSG_EXPIRED = "Your sign-in took too long and expired. Please try again."
MSG_UNREACHABLE = "The sign-in service isn't responding right now. Please try again in a moment."
MSG_SERVER = "Something went wrong on our side while signing you in. Please try again in a moment."


class Auth0Error(Exception):
    """
    Any Auth0 login failure. str(exc) is the technical reason, for the
    server log only; exc.user_message is what the user is shown (it
    defaults to the same text when that text is already user-friendly).
    Neither ever contains a token, code or secret.
    """

    def __init__(self, reason: str, user_message: str | None = None):
        super().__init__(reason)
        self.user_message = user_message or reason


_DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def config_problems() -> list[str]:
    """
    What's missing or malformed in the Auth0 settings, by variable NAME
    only -- values (and the client secret in particular) are never
    included, so this is safe to log.
    """
    problems = []
    if not AUTH0_DOMAIN:
        problems.append("AUTH0_DOMAIN is not set")
    elif not _DOMAIN_RE.match(AUTH0_DOMAIN):
        problems.append("AUTH0_DOMAIN is not a bare host name (e.g. tenant.us.auth0.com)")
    if not AUTH0_CLIENT_ID:
        problems.append("AUTH0_CLIENT_ID is not set")
    if not AUTH0_CLIENT_SECRET:
        problems.append("AUTH0_CLIENT_SECRET is not set")
    for name, value in (("AUTH0_CALLBACK_URL", AUTH0_CALLBACK_URL), ("AUTH0_LOGOUT_RETURN_URL", AUTH0_LOGOUT_RETURN_URL)):
        if not value.startswith(("http://", "https://")):
            problems.append(f"{name} must be an http(s) URL")
    return problems


def is_configured() -> bool:
    return not config_problems()


def issuer() -> str:
    return f"https://{AUTH0_DOMAIN}/"


def build_authorize_url(state: str, nonce: str, connection: str | None = None, extra: dict | None = None) -> str:
    query = {
        "response_type": "code",
        "client_id": AUTH0_CLIENT_ID,
        "redirect_uri": AUTH0_CALLBACK_URL,
        "scope": AUTH0_SCOPE,
        "state": state,
        "nonce": nonce,
    }
    if connection:
        # Skips Auth0's own login page and goes straight to that provider
        # (e.g. Google's account chooser). Auth0 still runs the login and
        # still redirects back to AUTH0_CALLBACK_URL as usual.
        query["connection"] = connection
    if extra:
        # Never lets a caller override the security-relevant parameters above.
        query.update({k: v for k, v in extra.items() if k not in query})
    params = httpx.QueryParams(query)
    return f"https://{AUTH0_DOMAIN}/authorize?{params}"


def build_logout_url() -> str:
    params = httpx.QueryParams({"client_id": AUTH0_CLIENT_ID, "returnTo": AUTH0_LOGOUT_RETURN_URL})
    return f"https://{AUTH0_DOMAIN}/v2/logout?{params}"


def exchange_code_for_tokens(code: str) -> dict:
    try:
        resp = httpx.post(
            f"https://{AUTH0_DOMAIN}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": AUTH0_CLIENT_ID,
                "client_secret": AUTH0_CLIENT_SECRET,
                "code": code,
                "redirect_uri": AUTH0_CALLBACK_URL,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise Auth0Error(f"Could not reach Auth0 token endpoint ({type(exc).__name__})", MSG_UNREACHABLE) from exc
    if resp.status_code != 200:
        # Auth0's error body is {"error": ..., "error_description": ...} --
        # no secrets -- and says e.g. invalid_grant (reused/expired code) or
        # access_denied/unauthorized_client (wrong client secret or ID).
        try:
            body = resp.json()
            detail = f"{body.get('error')}: {body.get('error_description')}"
        except ValueError:
            detail = "non-JSON body"
        raise Auth0Error(f"Auth0 rejected the login code (HTTP {resp.status_code}, {detail})", MSG_RETRY)
    try:
        tokens = resp.json()
    except ValueError as exc:
        raise Auth0Error("Auth0 token endpoint returned a non-JSON response", MSG_RETRY) from exc
    if not isinstance(tokens, dict) or not tokens.get("id_token"):
        raise Auth0Error("Auth0 token response had no id_token", MSG_RETRY)
    return tokens


def fetch_jwks() -> dict:
    try:
        resp = httpx.get(f"https://{AUTH0_DOMAIN}/.well-known/jwks.json", timeout=HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise Auth0Error(f"Could not fetch Auth0 signing keys ({type(exc).__name__})", MSG_UNREACHABLE) from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise Auth0Error("Auth0 signing keys response was not JSON", MSG_UNREACHABLE) from exc


def _signing_key(kid: str | None) -> dict:
    global _jwks_cache
    for refresh in (False, True):
        if _jwks_cache is None or refresh:
            _jwks_cache = fetch_jwks()
        for key in _jwks_cache.get("keys", []):
            if key.get("kid") == kid:
                return key
    raise Auth0Error("Unknown Auth0 signing key", MSG_VERIFY)


def verify_id_token(id_token: str, nonce: str, access_token: str | None = None) -> dict:
    try:
        header = jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise Auth0Error("Malformed Auth0 ID token", MSG_VERIFY) from exc
    if header.get("alg") != "RS256":
        raise Auth0Error(f"Unexpected Auth0 token algorithm {header.get('alg')!r}", MSG_VERIFY)

    try:
        claims = jwt.decode(
            id_token,
            _signing_key(header.get("kid")),
            algorithms=["RS256"],
            audience=AUTH0_CLIENT_ID,
            issuer=issuer(),
            access_token=access_token,
        )
    except ExpiredSignatureError as exc:
        raise Auth0Error("Auth0 ID token expired", MSG_EXPIRED) from exc
    except JWTError as exc:
        # Bad signature, wrong issuer or audience (e.g. token for another app).
        raise Auth0Error(f"Invalid Auth0 ID token ({exc})", MSG_VERIFY) from exc

    if not claims.get("sub"):
        raise Auth0Error("Auth0 ID token has no subject", MSG_VERIFY)
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise Auth0Error("Auth0 login nonce mismatch", MSG_VERIFY)
    return claims


def auth0_provider(sub: str) -> str:
    """
    The identity provider behind an Auth0 user id: "auth0" (Auth0's own
    email/password database), "google-oauth2", etc. Auth0 always encodes it
    as the part of "sub" before the "|", so it's derived here rather than
    stored in a second column that could drift out of sync with auth0_sub.
    """
    provider, sep, _ = sub.partition("|")
    return provider if sep else "unknown"


# Display names for user-facing messages; anything else reads "your account".
_PROVIDER_LABELS = {"google-oauth2": "Google", "facebook": "Facebook", "auth0": "Auth0"}


def _unique_username(db: Session, claims: dict) -> str:
    email = claims.get("email") or ""
    raw = claims.get("nickname") or claims.get("name") or email.split("@")[0] or "auth0user"
    base = re.sub(r"[^A-Za-z0-9_.-]", "", raw)[:40] or "auth0user"
    candidate = base
    suffix = 1
    while db.query(models.User).filter(models.User.username == candidate).first():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def linked_subs(db: Session, user_id: int) -> set[str]:
    """Every Auth0 identity linked to a user (Google, Facebook, ...)."""
    rows = db.query(models.UserAuth0Identity.auth0_sub).filter(models.UserAuth0Identity.user_id == user_id)
    return {sub for (sub,) in rows}


def _find_linked_user(db: Session, sub: str) -> models.User | None:
    identity = db.query(models.UserAuth0Identity).filter(models.UserAuth0Identity.auth0_sub == sub).first()
    if identity is not None:
        return identity.user
    # Linked before user_auth0_identities existed (users.auth0_sub only) and
    # not backfilled yet -- still the same person.
    return db.query(models.User).filter(models.User.auth0_sub == sub).first()


def get_or_create_user(db: Session, claims: dict) -> models.User:
    """
    Maps verified Auth0 claims onto a local user:

    1. A user already linked to this Auth0 "sub" -> that user.
    2. Else a local user with the same email -> this identity is linked to
       them as an additional login (a user can have several: Google,
       Facebook, ...), but only when Auth0 says the email is verified --
       otherwise anyone able to create an unverified Auth0 identity with
       that address could take the account.
    3. Else a brand-new user on the Basic plan, exactly like /auth/register.
       Their password_hash is a hash of random bytes nobody knows, so they
       can only sign in through Auth0 -- password_hash stays NOT NULL.

    Linking only ever adds a user_auth0_identities row (and fills
    users.auth0_sub if it was still empty); the existing user's username,
    password_hash, plan and data are left exactly as they were, so their
    email/password login keeps working too.
    """
    sub = claims["sub"]
    provider = auth0_provider(sub)
    user = _find_linked_user(db, sub)
    if user is not None:
        if sub not in linked_subs(db, user.id):
            # Self-heal a users.auth0_sub-only link into the identities table.
            db.add(models.UserAuth0Identity(user_id=user.id, auth0_sub=sub))
            return _commit_or_reuse(db, sub) or user
        return user

    email = (claims.get("email") or "").strip()
    if not email:
        # Typical for Facebook: the email permission was declined, or the
        # account was registered with a phone number and has no email.
        label = _PROVIDER_LABELS.get(provider, "Your account")
        logger.warning("Auth0 %s login refused: no email claim", provider)
        raise Auth0Error(
            f"{label} didn't share an email address, which is needed to sign in. "
            "Allow email access when asked, or use another way to log in."
        )

    # Case-insensitive: Auth0 lowercases emails, but /auth/register stores
    # them as typed -- an exact match would miss "Alice@Example.com" and
    # silently create a second account for the same person.
    matches = db.query(models.User).filter(func.lower(models.User.email) == email.lower()).all()
    if len(matches) > 1:
        logger.warning("Auth0 %s login refused: %d local users share email case-insensitively", provider, len(matches))
        raise Auth0Error("More than one account uses this email. Please contact support to link your login.")

    if matches:
        existing = matches[0]
        if claims.get("email_verified") is not True:
            raise Auth0Error("Verify your email with Auth0 before signing in to an existing account")
        if existing.auth0_sub is None:
            existing.auth0_sub = sub
        db.add(models.UserAuth0Identity(user_id=existing.id, auth0_sub=sub))
        reused = _commit_or_reuse(db, sub)
        if reused is not None:
            return reused
        db.refresh(existing)
        logger.info(
            "Auth0 %s identity linked to existing user id=%s (%d linked logins)",
            provider, existing.id, len(linked_subs(db, existing.id)),
        )
        return existing

    basic_plan = subscription_service.get_or_create_basic_plan(db)
    user = models.User(
        username=_unique_username(db, claims),
        email=email,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        subscription_plan_id=basic_plan.id,
        auth0_sub=sub,
    )
    db.add(user)
    # The identity row needs user.id, so it's added after a flush -- inside
    # _commit_or_reuse, so a race tripping the flush is handled the same way.
    reused = _commit_or_reuse(db, sub, then=lambda: db.add(models.UserAuth0Identity(user_id=user.id, auth0_sub=sub)))
    if reused is not None:
        return reused
    db.refresh(user)
    logger.info("Auth0 %s identity created new user id=%s", provider, user.id)
    return user


def _commit_or_reuse(db: Session, sub: str, then=None) -> models.User | None:
    """
    Commits a link/create. If it trips a unique constraint -- typically two
    first logins for the same person racing each other -- rolls back and
    returns whichever user the other request already linked to `sub`,
    instead of failing or creating a duplicate.
    """
    try:
        if then is not None:
            db.flush()
            then()
        db.commit()
        return None
    except IntegrityError:
        db.rollback()
        winner = _find_linked_user(db, sub)
        if winner is None:
            raise Auth0Error(f"Unique-constraint conflict linking {auth0_provider(sub)} identity; no winner found", MSG_RETRY)
        return winner
