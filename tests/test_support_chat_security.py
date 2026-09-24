"""
AI Support Chat security -- a consolidated audit across every support chat
endpoint, in the same spirit as test_subscription_endpoint_security_audit.py:

  POST /api/ai-support/            GET /api/ai-support/history/
  POST /support-chat/ask           GET /support-chat/history

1. Authentication is required: every endpoint rejects missing, malformed,
   expired, forged, tampered and orphaned tokens with 401, and writes nothing.
2. User isolation (read): a user only ever sees their own exchanges, however
   the request is shaped.
3. User isolation (write): there is no way to edit or delete a chat record,
   and a client can't smuggle in someone else's user_id or a fake answer.
4. Every saved exchange belongs to the user the JWT identifies.
5. Empty and whitespace-only messages are rejected.
6. Over-long messages are rejected before anything is saved.
7. JWT login keeps working for these and existing endpoints.
8. Existing authorization rules (admin-only, post ownership, notification
   ownership) are unaffected.

No production code changes were needed: every support endpoint resolves the
caller exclusively through get_current_user (app/auth.py), and the request
schemas validate length and blankness (app/schemas.py).
"""

import base64
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.auth import ALGORITHM, create_access_token
from app.database import Base, get_db
from app.main import app as main_app

USER_A = {"username": "user_a", "email": "usera@example.com", "password": "Password123"}
USER_B = {"username": "user_b", "email": "userb@example.com", "password": "Password123"}

# (method, path, a valid body) for every support chat endpoint.
ENDPOINTS = [
    ("POST", "/api/ai-support/", {"message": "How do I create a post?"}),
    ("GET", "/api/ai-support/history/", None),
    ("POST", "/support-chat/ask", {"question": "How do I create a post?"}),
    ("GET", "/support-chat/history", None),
]
ENDPOINT_IDS = [f"{method} {path}" for method, path, _ in ENDPOINTS]

# (path, field name) for the two endpoints that accept a message.
ASK_ENDPOINTS = [("/api/ai-support/", "message"), ("/support-chat/ask", "question")]
ASK_IDS = [path for path, _ in ASK_ENDPOINTS]


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    main_app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_app) as client:
        yield client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, user: dict) -> tuple[dict, int]:
    client.post("/auth/register", json=user)
    token = client.post("/auth/login", json={"username": user["username"], "password": user["password"]}).json()[
        "access_token"
    ]
    headers = {"Authorization": f"Bearer {token}"}
    user_id = client.get("/auth/me", headers=headers).json()["id"]
    return headers, user_id


def _call(client: TestClient, method: str, path: str, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


def _chat_rows(session_factory) -> list[models.SupportChatMessage]:
    db = session_factory()
    try:
        return db.query(models.SupportChatMessage).order_by(models.SupportChatMessage.id).all()
    finally:
        db.close()


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# 1. Authentication is required
# ---------------------------------------------------------------------------

def _bad_auth_headers(user_id: int, valid_token: str, other_user_id: int) -> dict[str, dict]:
    header, payload, signature = valid_token.split(".")
    tampered_payload = json.loads(base64.urlsafe_b64decode(payload + "=="))
    tampered_payload["sub"] = str(other_user_id)
    return {
        "no header": {},
        "empty bearer": {"Authorization": "Bearer "},
        "wrong scheme": {"Authorization": "Basic dXNlcl9hOlBhc3N3b3JkMTIz"},
        "token without scheme": {"Authorization": valid_token},
        "garbage token": {"Authorization": "Bearer not.a.jwt"},
        "expired token": {
            "Authorization": "Bearer " + create_access_token({"sub": str(user_id)}, timedelta(seconds=-5))
        },
        "wrong signing key": {
            "Authorization": "Bearer " + jwt.encode({"sub": str(user_id)}, "attacker-secret", algorithm=ALGORITHM)
        },
        "alg none": {
            "Authorization": "Bearer " + _b64url({"alg": "none", "typ": "JWT"}) + "." + _b64url({"sub": str(user_id)}) + "."
        },
        "tampered sub (other user)": {"Authorization": f"Bearer {header}.{_b64url(tampered_payload)}.{signature}"},
        "user does not exist": {"Authorization": "Bearer " + create_access_token({"sub": "999999"})},
        "non-numeric sub": {"Authorization": "Bearer " + create_access_token({"sub": "user_a"})},
        "missing sub": {"Authorization": "Bearer " + create_access_token({"name": "user_a"})},
    }


@pytest.mark.parametrize("method, path, body", ENDPOINTS, ids=ENDPOINT_IDS)
def test_every_endpoint_rejects_every_kind_of_bad_token(env, method, path, body):
    client, session_factory = env
    headers_a, user_a_id = _login(client, USER_A)
    _, user_b_id = _login(client, USER_B)
    valid_token = headers_a["Authorization"].removeprefix("Bearer ")

    for label, headers in _bad_auth_headers(user_a_id, valid_token, user_b_id).items():
        resp = _call(client, method, path, body, headers)
        assert resp.status_code == 401, f"{label}: got {resp.status_code}"
        assert resp.json() == {"detail": "Could not validate credentials"}, label
        assert resp.headers.get("www-authenticate") == "Bearer", label

    assert _chat_rows(session_factory) == []


def test_token_stops_working_once_its_user_is_deleted(env):
    client, session_factory = env
    headers, user_id = _login(client, USER_A)
    assert client.post("/api/ai-support/", json={"message": "hi"}, headers=headers).status_code == 200

    db = session_factory()
    try:
        db.delete(db.get(models.User, user_id))
        db.commit()
    finally:
        db.close()

    for method, path, body in ENDPOINTS:
        assert _call(client, method, path, body, headers).status_code == 401, path


# ---------------------------------------------------------------------------
# 2. User A can't read User B's history
# ---------------------------------------------------------------------------

@pytest.fixture()
def two_users_with_history(env):
    client, session_factory = env
    headers_a, a_id = _login(client, USER_A)
    headers_b, b_id = _login(client, USER_B)
    for i in range(3):
        client.post("/api/ai-support/", json={"message": f"A secret {i}"}, headers=headers_a)
        client.post("/support-chat/ask", json={"question": f"B secret {i}"}, headers=headers_b)
    return client, session_factory, (headers_a, a_id), (headers_b, b_id)


def _all_questions(client, headers, params=None) -> list[str]:
    new = client.get("/api/ai-support/history/", params={"limit": 100, **(params or {})}, headers=headers).json()
    old = client.get("/support-chat/history", params=params or {}, headers=headers).json()
    return [m["question"] for m in new["messages"]] + [m["question"] for m in old["messages"]]


def test_each_user_sees_only_their_own_history(two_users_with_history):
    client, _, (headers_a, _), (headers_b, _) = two_users_with_history

    a_questions = _all_questions(client, headers_a)
    b_questions = _all_questions(client, headers_b)
    assert a_questions and all(q.startswith("A secret") for q in a_questions)
    assert b_questions and all(q.startswith("B secret") for q in b_questions)
    assert client.get("/api/ai-support/history/", headers=headers_a).json()["total"] == 3


@pytest.mark.parametrize(
    "params",
    [
        {"user_id": 2},
        {"user_id": "2"},
        {"username": "user_b"},
        {"owner": "user_b"},
        {"user": 2, "all": "true"},
        {"page": 1, "limit": 100, "user_id": 2},
    ],
)
def test_query_parameters_cannot_select_another_user(two_users_with_history, params):
    client, _, (headers_a, _), (_, b_id) = two_users_with_history
    params = {k: (b_id if v == 2 else v) for k, v in params.items()}
    assert all(q.startswith("A secret") for q in _all_questions(client, headers_a, params))


@pytest.mark.parametrize(
    "path",
    [
        "/api/ai-support/history/1",
        "/api/ai-support/history/2/",
        "/api/ai-support/1",
        "/support-chat/history/1",
        "/support-chat/2",
        "/api/ai-support/history/../history/",
    ],
)
def test_there_is_no_by_id_read_path(two_users_with_history, path):
    client, _, (headers_a, _), _ = two_users_with_history
    resp = client.get(path, headers=headers_a)
    assert resp.status_code in (404, 405) or all(
        q.startswith("A secret") for q in [m["question"] for m in resp.json().get("messages", [])]
    ), path
    assert "B secret" not in resp.text


def test_support_routes_take_no_path_parameters():
    spec = main_app.openapi()
    support_paths = [p for p in spec["paths"] if p.startswith(("/api/ai-support", "/support-chat"))]
    assert sorted(support_paths) == ["/api/ai-support/", "/api/ai-support/history/", "/support-chat/ask", "/support-chat/history"]
    assert not any("{" in p for p in support_paths)
    for path in support_paths:
        for operation in spec["paths"][path].values():
            names = {p["name"] for p in operation.get("parameters", [])}
            assert not names & {"user_id", "username", "owner", "id"}, path


def test_history_responses_never_include_user_ids(two_users_with_history):
    client, _, (headers_a, a_id), _ = two_users_with_history
    for path in ("/api/ai-support/history/", "/support-chat/history"):
        for message in client.get(path, headers=headers_a).json()["messages"]:
            assert "user_id" not in message and "username" not in message, path


# ---------------------------------------------------------------------------
# 3. User A can't modify User B's records
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/ai-support/",
        "/api/ai-support/history/",
        "/support-chat/ask",
        "/support-chat/history",
        "/api/ai-support/history/1",
        "/support-chat/history/1",
    ],
)
def test_no_route_edits_or_deletes_chat_records(two_users_with_history, method, path):
    client, session_factory, (headers_a, _), _ = two_users_with_history
    before = [(r.id, r.user_id, r.question, r.response) for r in _chat_rows(session_factory)]

    resp = client.request(method, path, json={"question": "hacked", "response": "hacked"}, headers=headers_a)
    assert resp.status_code in (404, 405), f"{method} {path} -> {resp.status_code}"
    assert [(r.id, r.user_id, r.question, r.response) for r in _chat_rows(session_factory)] == before


def test_support_routes_only_allow_expected_methods():
    spec = main_app.openapi()
    assert set(spec["paths"]["/api/ai-support/"]) == {"post"}
    assert set(spec["paths"]["/api/ai-support/history/"]) == {"get"}
    assert set(spec["paths"]["/support-chat/ask"]) == {"post"}
    assert set(spec["paths"]["/support-chat/history"]) == {"get"}


@pytest.mark.parametrize("path, field", ASK_ENDPOINTS, ids=ASK_IDS)
def test_client_cannot_set_owner_id_or_answer(two_users_with_history, path, field):
    client, session_factory, (headers_a, a_id), (_, b_id) = two_users_with_history
    b_rows_before = [(r.id, r.question, r.response) for r in _chat_rows(session_factory) if r.user_id == b_id]
    existing_b_id = b_rows_before[0][0]

    resp = client.post(
        path,
        json={
            field: "Where is my invoice?",
            "user_id": b_id,
            "username": "user_b",
            "id": existing_b_id,
            "response": "Attacker-chosen answer",
            "ai_response": "Attacker-chosen answer",
            "response_source": "claude",
            "created_at": "2000-01-01T00:00:00Z",
        },
        headers=headers_a,
    )
    assert resp.status_code in (200, 201)
    assert "Attacker-chosen" not in resp.text

    newest = _chat_rows(session_factory)[-1]
    assert newest.user_id == a_id
    assert newest.id != existing_b_id
    assert newest.response != "Attacker-chosen answer"
    assert newest.response_source == "predefined"
    assert newest.created_at.year != 2000
    # B's records are exactly as they were.
    assert [(r.id, r.question, r.response) for r in _chat_rows(session_factory) if r.user_id == b_id] == b_rows_before


# ---------------------------------------------------------------------------
# 4. Messages are tied to the authenticated user
# ---------------------------------------------------------------------------

def test_every_saved_message_belongs_to_the_token_user(two_users_with_history):
    _, session_factory, (_, a_id), (_, b_id) = two_users_with_history
    rows = _chat_rows(session_factory)
    assert len(rows) == 6
    for row in rows:
        assert row.user_id == (a_id if row.question.startswith("A secret") else b_id)


def test_switching_tokens_switches_owner(env):
    client, session_factory = env
    headers_a, a_id = _login(client, USER_A)
    headers_b, b_id = _login(client, USER_B)
    client.post("/api/ai-support/", json={"message": "first"}, headers=headers_a)
    client.post("/api/ai-support/", json={"message": "second"}, headers=headers_b)
    client.post("/api/ai-support/", json={"message": "third"}, headers=headers_a)
    assert [(r.question, r.user_id) for r in _chat_rows(session_factory)] == [
        ("first", a_id),
        ("second", b_id),
        ("third", a_id),
    ]


# ---------------------------------------------------------------------------
# 5. Empty messages are rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, field", ASK_ENDPOINTS, ids=ASK_IDS)
@pytest.mark.parametrize(
    "value",
    ["", " ", "     ", "\n", "\t\n  \r\n", " ", "  ", "　"],
    ids=["empty", "space", "spaces", "newline", "mixed-whitespace", "nbsp", "em-space", "ideographic-space"],
)
def test_blank_messages_are_rejected(env, path, field, value):
    client, session_factory = env
    headers, _ = _login(client, USER_A)
    resp = client.post(path, json={field: value}, headers=headers)
    assert resp.status_code == 422
    assert _chat_rows(session_factory) == []


@pytest.mark.parametrize("path, field", ASK_ENDPOINTS, ids=ASK_IDS)
@pytest.mark.parametrize("body_kind", ["missing", "null", "number", "list", "object", "bool"])
def test_missing_or_non_text_messages_are_rejected(env, path, field, body_kind):
    client, session_factory = env
    headers, _ = _login(client, USER_A)
    body = {
        "missing": {},
        "null": {field: None},
        "number": {field: 42},
        "list": {field: ["How do I create a post?"]},
        "object": {field: {"text": "How do I create a post?"}},
        "bool": {field: True},
    }[body_kind]
    assert client.post(path, json=body, headers=headers).status_code == 422
    assert _chat_rows(session_factory) == []


# ---------------------------------------------------------------------------
# 6. Over-long messages are rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path, field", ASK_ENDPOINTS, ids=ASK_IDS)
@pytest.mark.parametrize("length", [2001, 5000, 100_000, 1_000_000])
def test_over_long_messages_are_rejected_and_not_saved(env, path, field, length):
    client, session_factory = env
    headers, _ = _login(client, USER_A)
    resp = client.post(path, json={field: "x" * length}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "string_too_long"
    assert _chat_rows(session_factory) == []


@pytest.mark.parametrize("path, field", ASK_ENDPOINTS, ids=ASK_IDS)
def test_limit_is_exactly_2000_characters_including_multibyte(env, path, field):
    client, session_factory = env
    headers, _ = _login(client, USER_A)
    assert client.post(path, json={field: "x" * 2000}, headers=headers).status_code in (200, 201)
    # Counted in characters, not bytes: 2000 four-byte emoji are accepted, 2001 are not.
    assert client.post(path, json={field: "\U0001F600" * 2000}, headers=headers).status_code in (200, 201)
    assert client.post(path, json={field: "\U0001F600" * 2001}, headers=headers).status_code == 422
    assert len(_chat_rows(session_factory)) == 2


def test_padding_does_not_bypass_blank_check_or_limit(env):
    client, _ = env
    headers, _ = _login(client, USER_A)
    # Whitespace is trimmed before the blank check...
    assert client.post("/api/ai-support/", json={"message": " " * 1999}, headers=headers).status_code == 422
    # ...but the length limit applies to what was sent, so padding can't sneak extra text through.
    assert client.post("/api/ai-support/", json={"message": "x" * 1990 + " " * 11}, headers=headers).status_code == 422


def test_markup_is_stored_and_returned_as_plain_json_text(env):
    client, session_factory = env
    headers, _ = _login(client, USER_A)
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script>'
    resp = client.post("/api/ai-support/", json={"message": payload}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert _chat_rows(session_factory)[-1].question == payload  # stored verbatim; the widget renders it as text
    history = client.get("/api/ai-support/history/", headers=headers)
    assert history.headers["content-type"].startswith("application/json")
    assert history.json()["messages"][0]["question"] == payload


# ---------------------------------------------------------------------------
# 7. JWT authentication keeps working
# ---------------------------------------------------------------------------

def test_login_token_works_for_support_and_existing_endpoints(env):
    client, _ = env
    headers, user_id = _login(client, USER_A)

    assert client.get("/auth/me", headers=headers).json()["username"] == "user_a"
    assert client.get("/dashboard/me", headers=headers).status_code == 200
    assert client.get("/notifications/", headers=headers).status_code == 200
    assert client.get("/subscriptions/me", headers=headers).status_code == 200
    for method, path, body in ENDPOINTS:
        assert _call(client, method, path, body, headers).status_code in (200, 201), path


def test_wrong_password_still_gets_no_token(env):
    client, _ = env
    _login(client, USER_A)
    resp = client.post("/auth/login", json={"username": "user_a", "password": "WrongPassword1"})
    assert resp.status_code == 401
    assert "access_token" not in resp.json()


def test_each_login_token_identifies_its_own_user(env):
    client, _ = env
    headers_a, a_id = _login(client, USER_A)
    headers_b, b_id = _login(client, USER_B)
    assert a_id != b_id
    assert client.get("/auth/me", headers=headers_a).json()["id"] == a_id
    assert client.get("/auth/me", headers=headers_b).json()["id"] == b_id


# ---------------------------------------------------------------------------
# 8. Existing authorization rules are unchanged
# ---------------------------------------------------------------------------

def test_admin_routes_still_forbidden_to_regular_users(env):
    client, _ = env
    headers, _ = _login(client, USER_A)
    for path in ("/admin/plans", "/admin/subscriptions", "/admin/billing-history"):
        assert client.get(path, headers=headers).status_code == 403, path
        assert client.get(path).status_code == 401, path


def test_post_ownership_still_enforced(env):
    client, _ = env
    headers_a, _ = _login(client, USER_A)
    headers_b, _ = _login(client, USER_B)
    post_id = client.post("/posts", json={"title": "A's post", "content": "Mine"}, headers=headers_a).json()["id"]

    assert client.put(f"/posts/{post_id}", json={"title": "Hijacked"}, headers=headers_b).status_code == 403
    assert client.delete(f"/posts/{post_id}", headers=headers_b).status_code == 403
    assert client.get(f"/posts/{post_id}").json()["title"] == "A's post"


def test_notification_ownership_still_enforced(env):
    client, session_factory = env
    headers_a, a_id = _login(client, USER_A)
    headers_b, _ = _login(client, USER_B)
    db = session_factory()
    try:
        notification = models.Notification(user_id=a_id, message="For A only", notification_type="like")
        db.add(notification)
        db.commit()
        notification_id = notification.id
    finally:
        db.close()

    assert client.patch(f"/notifications/{notification_id}/read", headers=headers_b).status_code == 404
    assert client.get("/notifications/", headers=headers_b).json()["notifications"] == []
    assert client.get("/notifications/", headers=headers_a).json()["unread_count"] == 1


def test_support_chat_adds_no_unauthenticated_routes():
    # Every route this feature added requires the bearer token.
    spec = main_app.openapi()
    for path in ("/api/ai-support/", "/api/ai-support/history/", "/support-chat/ask", "/support-chat/history"):
        for operation in spec["paths"][path].values():
            assert operation.get("security") == [{"HTTPBearer": []}], path
