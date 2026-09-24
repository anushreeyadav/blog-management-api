"""
HTTP-level tests for POST /api/ai-support/ and GET /api/ai-support/history/,
wired into app.main.app.

The endpoint is a thin wrapper over app/services/support_chat.py, whose own
behaviour (Claude vs. FAQ fallback, history) is covered in
tests/test_support_chat_api.py and tests/test_support_faq.py -- these tests
focus on this endpoint's contract: auth, the {"message"} ->
{"response", "timestamp"} shape, the saved record, and input validation.
conftest.py keeps the real Anthropic API off.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import Base, get_db
from app.main import app as main_app
from app.services import support_chat as support_chat_service
from app.services import support_faq

URL = "/api/ai-support/"


@pytest.fixture()
def auth_client():
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
    with TestClient(main_app) as test_client:
        yield test_client, TestingSessionLocal
    main_app.dependency_overrides.clear()
    engine.dispose()


def _register_and_login(client: TestClient, username: str = "alice", password: str = "strongpass123") -> dict:
    client.post("/auth/register", json={"username": username, "email": f"{username}@example.com", "password": password})
    resp = client.post("/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_requires_authentication(auth_client):
    client, _ = auth_client
    resp = client.post(URL, json={"message": "How do I create a post?"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Could not validate credentials"}


def test_rejects_invalid_token(auth_client):
    client, _ = auth_client
    resp = client.post(URL, json={"message": "How do I create a post?"}, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

def test_returns_faq_response(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, json={"message": "How do I create a post?"}, headers=headers)

    assert resp.status_code == 200
    assert set(resp.json()) == {"response", "timestamp"}
    assert resp.json()["response"] == support_faq.get_support_response("How do I create a post?").response
    assert resp.json()["response"].startswith("To create a post")


def test_unknown_message_returns_fallback(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, json={"message": "Tell me a joke"}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["response"] == support_faq.FALLBACK_RESPONSE


def test_returns_claude_response_when_ai_enabled(auth_client, monkeypatch):
    client, _ = auth_client
    monkeypatch.setattr(support_chat_service, "AI_CHAT_ENABLED", True)
    text_block = SimpleNamespace(type="text", text="Claude says: use POST /posts.")
    fake_response = SimpleNamespace(stop_reason="end_turn", content=[text_block])
    fake_client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **_: fake_response)))
    monkeypatch.setattr(support_chat_service, "_client", fake_client)
    headers = _register_and_login(client)

    resp = client.post(URL, json={"message": "How do I create a post?"}, headers=headers)
    assert resp.json()["response"] == "Claude says: use POST /posts."


def test_message_is_trimmed_and_exchange_saved_to_history(auth_client):
    client, session_factory = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, json={"message": "  How do I delete a post?  "}, headers=headers)

    history = client.get("/support-chat/history", headers=headers).json()["messages"]
    assert [m["question"] for m in history] == ["How do I delete a post?"]
    assert history[0]["response"] == resp.json()["response"]

    db = session_factory()
    try:
        assert db.query(models.SupportChatMessage).count() == 1
    finally:
        db.close()


def test_path_without_trailing_slash_also_works(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.post("/api/ai-support", json={"message": "How do I create a post?"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["response"].startswith("To create a post")


# ---------------------------------------------------------------------------
# Saved records
# ---------------------------------------------------------------------------

def _user_id(session_factory, username: str) -> int:
    db = session_factory()
    try:
        return db.query(models.User).filter(models.User.username == username).one().id
    finally:
        db.close()


def test_successful_request_creates_database_record(auth_client):
    client, session_factory = auth_client
    headers = _register_and_login(client, "alice")
    resp = client.post(URL, json={"message": "How do I create a post?"}, headers=headers)
    assert resp.status_code == 200

    db = session_factory()
    try:
        record = db.query(models.SupportChatMessage).one()
        assert record.user_id == _user_id(session_factory, "alice")
        assert record.question == "How do I create a post?"
        assert record.response == resp.json()["response"]
        assert record.response_source == "predefined"
        assert record.created_at is not None
        # The returned timestamp is the saved record's timestamp.
        assert datetime.fromisoformat(resp.json()["timestamp"]) == record.created_at
    finally:
        db.close()


def test_each_request_creates_one_record_for_the_caller(auth_client):
    client, session_factory = auth_client
    alice = _register_and_login(client, "alice")
    bob = _register_and_login(client, "bob")
    client.post(URL, json={"message": "Alice one"}, headers=alice)
    client.post(URL, json={"message": "Alice two"}, headers=alice)
    client.post(URL, json={"message": "Bob one"}, headers=bob)

    db = session_factory()
    try:
        by_user = {}
        for record in db.query(models.SupportChatMessage).order_by(models.SupportChatMessage.id):
            by_user.setdefault(record.user_id, []).append(record.question)
    finally:
        db.close()
    assert by_user == {
        _user_id(session_factory, "alice"): ["Alice one", "Alice two"],
        _user_id(session_factory, "bob"): ["Bob one"],
    }


def test_response_never_includes_other_users_records(auth_client):
    client, _ = auth_client
    alice = _register_and_login(client, "alice")
    bob = _register_and_login(client, "bob")
    client.post(URL, json={"message": "Alice's private billing question"}, headers=alice)

    resp = client.post(URL, json={"message": "How do I create a post?"}, headers=bob)
    assert set(resp.json()) == {"response", "timestamp"}
    assert "Alice" not in resp.text

    bob_history = client.get("/support-chat/history", headers=bob).json()["messages"]
    assert [m["question"] for m in bob_history] == ["How do I create a post?"]


def test_unauthenticated_request_saves_nothing(auth_client):
    client, session_factory = auth_client
    client.post(URL, json={"message": "How do I create a post?"})

    db = session_factory()
    try:
        assert db.query(models.SupportChatMessage).count() == 0
    finally:
        db.close()


def test_failed_save_returns_error_and_leaves_no_record(auth_client, monkeypatch):
    client, session_factory = auth_client
    headers = _register_and_login(client, "alice")

    def failing_commit(self):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("sqlalchemy.orm.Session.commit", failing_commit)
    no_raise_client = TestClient(main_app, raise_server_exceptions=False)
    resp = no_raise_client.post(URL, json={"message": "How do I create a post?"}, headers=headers)
    monkeypatch.undo()

    assert resp.status_code == 500
    assert "database unavailable" not in resp.text
    db = session_factory()
    try:
        assert db.query(models.SupportChatMessage).count() == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 2001},
        {},
        {"question": "How do I create a post?"},
        {"message": None},
        {"message": 123},
    ],
    ids=["empty", "blank", "too-long", "missing", "wrong-field", "null", "not-a-string"],
)
def test_invalid_message_returns_422(auth_client, body):
    client, session_factory = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, json=body, headers=headers)

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"][:2] == ["body", "message"]

    # Nothing is stored for a rejected message.
    db = session_factory()
    try:
        assert db.query(models.SupportChatMessage).count() == 0
    finally:
        db.close()


def test_blank_message_error_is_readable(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, json={"message": "   "}, headers=headers)
    assert "message cannot be blank" in resp.json()["detail"][0]["msg"]


def test_malformed_json_returns_422(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, content="{not json", headers={**headers, "Content-Type": "application/json"})
    assert resp.status_code == 422


def test_max_length_message_is_accepted(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.post(URL, json={"message": "post " + "x" * 1995}, headers=headers)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Swagger / OpenAPI
# ---------------------------------------------------------------------------

def test_endpoint_is_documented_in_openapi(auth_client):
    client, _ = auth_client
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/api/ai-support/"]["post"]

    assert operation["tags"] == ["ai-support"]
    assert operation["security"] == [{"HTTPBearer": []}]
    request_schema = spec["components"]["schemas"]["AiSupportRequest"]
    assert request_schema["properties"]["message"]["maxLength"] == 2000
    assert request_schema["properties"]["message"]["minLength"] == 1
    response_schema = spec["components"]["schemas"]["AiSupportResponse"]
    assert set(response_schema["properties"]) == {"response", "timestamp"}
    assert response_schema["properties"]["timestamp"]["format"] == "date-time"


# ---------------------------------------------------------------------------
# GET /api/ai-support/history/
# ---------------------------------------------------------------------------

HISTORY_URL = "/api/ai-support/history/"


def _ask(client: TestClient, headers: dict, *messages: str) -> list[dict]:
    return [client.post(URL, json={"message": m}, headers=headers).json() for m in messages]


def test_history_requires_authentication(auth_client):
    client, _ = auth_client
    assert client.get(HISTORY_URL).status_code == 401
    assert client.get(HISTORY_URL, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_history_is_empty_for_new_user(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.get(HISTORY_URL, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"messages": [], "page": 1, "limit": 10, "total": 0, "total_pages": 0}


def test_history_returns_expected_fields_newest_first(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    answers = _ask(client, headers, "How do I create a post?", "How do I delete a post?", "Where is my invoice?")

    messages = client.get(HISTORY_URL, headers=headers).json()["messages"]
    assert [m["question"] for m in messages] == [
        "Where is my invoice?",
        "How do I delete a post?",
        "How do I create a post?",
    ]
    assert all(set(m) == {"id", "question", "ai_response", "created_at"} for m in messages)
    assert messages[0]["ai_response"] == answers[2]["response"]
    assert messages[0]["created_at"] == answers[2]["timestamp"]
    assert messages[0]["id"] > messages[1]["id"] > messages[2]["id"]


def test_history_only_returns_callers_own_messages(auth_client):
    client, _ = auth_client
    alice = _register_and_login(client, "alice")
    bob = _register_and_login(client, "bob")
    _ask(client, alice, "Alice's private billing question", "Alice again")
    _ask(client, bob, "Bob's question")

    alice_body = client.get(HISTORY_URL, headers=alice).json()
    bob_body = client.get(HISTORY_URL, headers=bob).json()
    assert [m["question"] for m in alice_body["messages"]] == ["Alice again", "Alice's private billing question"]
    assert alice_body["total"] == 2
    assert [m["question"] for m in bob_body["messages"]] == ["Bob's question"]
    assert bob_body["total"] == 1
    assert "Alice" not in client.get(HISTORY_URL, headers=bob).text


def test_history_cannot_be_requested_for_another_user(auth_client):
    client, _ = auth_client
    alice = _register_and_login(client, "alice")
    bob = _register_and_login(client, "bob")
    _ask(client, alice, "Alice's question")

    # Query parameters that might look like a user selector are simply ignored.
    resp = client.get(HISTORY_URL, params={"user_id": 1, "username": "alice"}, headers=bob)
    assert resp.json()["messages"] == []


def test_history_includes_chat_widget_messages(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    client.post("/support-chat/ask", json={"question": "Asked from the widget"}, headers=headers)
    _ask(client, headers, "Asked from the API")

    questions = [m["question"] for m in client.get(HISTORY_URL, headers=headers).json()["messages"]]
    assert questions == ["Asked from the API", "Asked from the widget"]


def test_history_pagination(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    _ask(client, headers, *[f"Question {i}" for i in range(1, 13)])  # 12 messages

    first = client.get(HISTORY_URL, headers=headers).json()
    assert (first["page"], first["limit"], first["total"], first["total_pages"]) == (1, 10, 12, 2)
    assert [m["question"] for m in first["messages"]] == [f"Question {i}" for i in range(12, 2, -1)]

    second = client.get(HISTORY_URL, params={"page": 2}, headers=headers).json()
    assert [m["question"] for m in second["messages"]] == ["Question 2", "Question 1"]

    custom = client.get(HISTORY_URL, params={"page": 3, "limit": 5}, headers=headers).json()
    assert (custom["total_pages"], len(custom["messages"])) == (3, 2)

    beyond = client.get(HISTORY_URL, params={"page": 9}, headers=headers).json()
    assert beyond["messages"] == [] and beyond["total"] == 12


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page": -1}, {"limit": 0}, {"limit": 101}, {"page": "abc"}],
    ids=["page-0", "page-negative", "limit-0", "limit-101", "page-not-a-number"],
)
def test_history_rejects_invalid_pagination(auth_client, params):
    client, _ = auth_client
    headers = _register_and_login(client)
    resp = client.get(HISTORY_URL, params=params, headers=headers)
    assert resp.status_code == 422


def test_history_path_without_trailing_slash_also_works(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client)
    _ask(client, headers, "How do I create a post?")
    resp = client.get("/api/ai-support/history", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_history_is_documented_in_openapi(auth_client):
    client, _ = auth_client
    spec = client.get("/openapi.json").json()
    operation = spec["paths"]["/api/ai-support/history/"]["get"]

    assert operation["tags"] == ["ai-support"]
    assert operation["security"] == [{"HTTPBearer": []}]
    assert {p["name"] for p in operation["parameters"]} == {"page", "limit"}
    item = spec["components"]["schemas"]["AiSupportHistoryItem"]
    assert set(item["properties"]) == {"id", "question", "ai_response", "created_at"}
