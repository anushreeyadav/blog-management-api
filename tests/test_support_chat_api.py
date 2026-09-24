"""
HTTP-level tests for the AI Support Chat (POST /support-chat/ask and
GET /support-chat/history), wired into app.main.app.

The real Anthropic API is never called: conftest.py's autouse
_disable_real_ai_chat fixture keeps AI_CHAT_ENABLED off, so by default every
answer comes from the predefined FAQs. Tests for the Claude path turn it back
on and swap in _FakeAnthropicClient, which records each request and returns
(or raises) whatever the test sets up.
"""

from types import SimpleNamespace

import anthropic
import httpx2
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


def _register_and_login(client: TestClient, username: str, email: str, password: str = "strongpass123") -> dict:
    client.post("/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/auth/login", json={"username": username, "password": password})
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class _FakeAnthropicClient:
    """Stands in for anthropic.Anthropic: records every messages.create call."""

    def __init__(self, *, text="Claude's answer", stop_reason="end_turn", error=None):
        self.calls = []
        self._text = text
        self._stop_reason = stop_reason
        self._error = error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        content = [SimpleNamespace(type="thinking", thinking="")]
        if self._text:
            content.append(SimpleNamespace(type="text", text=self._text))
        return SimpleNamespace(stop_reason=self._stop_reason, content=content)


@pytest.fixture()
def enable_claude(monkeypatch):
    """Turns the Claude path on with a fake client; returns a setter for it."""
    monkeypatch.setattr(support_chat_service, "AI_CHAT_ENABLED", True)

    def use(fake: _FakeAnthropicClient) -> _FakeAnthropicClient:
        monkeypatch.setattr(support_chat_service, "_client", fake)
        return fake

    return use


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_ask_requires_authentication(auth_client):
    client, _ = auth_client
    resp = client.post("/support-chat/ask", json={"question": "What plans are there?"})
    assert resp.status_code == 401


def test_history_requires_authentication(auth_client):
    client, _ = auth_client
    resp = client.get("/support-chat/history")
    assert resp.status_code == 401


def test_ask_rejects_invalid_token(auth_client):
    client, _ = auth_client
    resp = client.post(
        "/support-chat/ask",
        json={"question": "What plans are there?"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question", ["", "   ", "x" * 2001])
def test_ask_rejects_blank_or_too_long_question(auth_client, question):
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    resp = client.post("/support-chat/ask", json={"question": question}, headers=headers)
    assert resp.status_code == 422


def test_ask_strips_surrounding_whitespace(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    resp = client.post("/support-chat/ask", json={"question": "  where is my invoice?  "}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["question"] == "where is my invoice?"


# ---------------------------------------------------------------------------
# Predefined FAQ answers (AI disabled -- the default)
# ---------------------------------------------------------------------------

def test_ask_with_ai_disabled_returns_matching_faq_answer(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    resp = client.post("/support-chat/ask", json={"question": "What plans do you offer?"}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["response_source"] == "predefined"
    assert "Basic" in body["response"] and "Premium" in body["response"] and "Pro" in body["response"]
    assert body["question"] == "What plans do you offer?"
    assert body["id"] > 0
    assert body["created_at"]


def test_ask_with_no_matching_faq_returns_generic_fallback(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    resp = client.post("/support-chat/ask", json={"question": "Tell me a joke"}, headers=headers)

    assert resp.status_code == 201
    assert resp.json()["response"] == support_faq.FALLBACK_RESPONSE
    assert resp.json()["response_source"] == "predefined"


def test_ask_with_ai_disabled_uses_support_faq_answer(auth_client):
    # Topic matching itself is covered in tests/test_support_faq.py; this
    # only checks the endpoint returns exactly what that service answers.
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    question = "How can I edit my post?"
    resp = client.post("/support-chat/ask", json={"question": question}, headers=headers)
    assert resp.json()["response"] == support_faq.get_support_response(question).response


def test_ask_does_not_call_claude_when_ai_disabled(auth_client, monkeypatch):
    client, _ = auth_client
    fake = _FakeAnthropicClient()
    monkeypatch.setattr(support_chat_service, "_client", fake)
    headers = _register_and_login(client, "alice", "alice@example.com")

    client.post("/support-chat/ask", json={"question": "What plans are there?"}, headers=headers)
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Claude answers (AI enabled, fake client)
# ---------------------------------------------------------------------------

def test_ask_with_ai_enabled_returns_claude_answer(auth_client, enable_claude):
    client, _ = auth_client
    fake = enable_claude(_FakeAnthropicClient(text="You're on the Basic plan."))
    headers = _register_and_login(client, "alice", "alice@example.com")

    resp = client.post("/support-chat/ask", json={"question": "Which plan am I on?"}, headers=headers)

    assert resp.status_code == 201
    assert resp.json()["response"] == "You're on the Basic plan."
    assert resp.json()["response_source"] == "claude"
    assert len(fake.calls) == 1


def test_claude_request_carries_faqs_account_context_and_question(auth_client, enable_claude):
    client, _ = auth_client
    fake = enable_claude(_FakeAnthropicClient())
    headers = _register_and_login(client, "alice", "alice@example.com")

    client.post("/support-chat/ask", json={"question": "Which plan am I on?"}, headers=headers)

    request = fake.calls[0]
    assert request["model"] == support_chat_service.ANTHROPIC_MODEL
    system_text = "\n".join(block["text"] for block in request["system"])
    for topic in support_faq.FAQ_TOPICS:
        assert topic.question in system_text and topic.answer in system_text
    assert "Username: alice" in system_text
    assert "Current plan: Basic" in system_text
    assert request["messages"] == [{"role": "user", "content": "Which plan am I on?"}]


def test_claude_request_includes_previous_exchanges(auth_client, enable_claude):
    client, _ = auth_client
    fake = enable_claude(_FakeAnthropicClient(text="First answer"))
    headers = _register_and_login(client, "alice", "alice@example.com")

    client.post("/support-chat/ask", json={"question": "First question"}, headers=headers)
    client.post("/support-chat/ask", json={"question": "Follow-up question"}, headers=headers)

    assert fake.calls[1]["messages"] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Follow-up question"},
    ]


def test_claude_request_never_includes_other_users_history(auth_client, enable_claude):
    client, _ = auth_client
    fake = enable_claude(_FakeAnthropicClient())
    alice = _register_and_login(client, "alice", "alice@example.com")
    bob = _register_and_login(client, "bob", "bob@example.com")

    client.post("/support-chat/ask", json={"question": "Alice's private question"}, headers=alice)
    client.post("/support-chat/ask", json={"question": "Bob's question"}, headers=bob)

    bob_request = fake.calls[1]
    assert bob_request["messages"] == [{"role": "user", "content": "Bob's question"}]
    assert "alice" not in "\n".join(block["text"] for block in bob_request["system"])


def _connection_error():
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.mark.parametrize(
    "fake_kwargs",
    [
        {"error": _connection_error()},
        {"error": RuntimeError("unexpected")},
        {"stop_reason": "refusal"},
        {"text": ""},
    ],
    ids=["connection-error", "unexpected-error", "refusal", "empty-reply"],
)
def test_ask_falls_back_to_predefined_when_claude_unavailable(auth_client, enable_claude, fake_kwargs):
    client, _ = auth_client
    enable_claude(_FakeAnthropicClient(**fake_kwargs))
    headers = _register_and_login(client, "alice", "alice@example.com")

    resp = client.post("/support-chat/ask", json={"question": "Where is my invoice?"}, headers=headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["response_source"] == "predefined"
    assert "/subscriptions/billing-history" in body["response"]
    assert "unexpected" not in body["response"]


# ---------------------------------------------------------------------------
# History and persistence
# ---------------------------------------------------------------------------

def test_ask_persists_exchange(auth_client):
    client, session_factory = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    resp = client.post("/support-chat/ask", json={"question": "Where is my invoice?"}, headers=headers)

    db = session_factory()
    try:
        stored = db.query(models.SupportChatMessage).one()
        user = db.query(models.User).filter(models.User.username == "alice").one()
        assert stored.id == resp.json()["id"]
        assert stored.user_id == user.id
        assert stored.question == "Where is my invoice?"
        assert stored.response == resp.json()["response"]
        assert stored.response_source == "predefined"
        assert stored.created_at is not None
    finally:
        db.close()


def test_history_is_empty_for_new_user(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    resp = client.get("/support-chat/history", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}


def test_history_returns_own_exchanges_oldest_first(auth_client):
    client, _ = auth_client
    headers = _register_and_login(client, "alice", "alice@example.com")
    for question in ["First", "Second", "Third"]:
        client.post("/support-chat/ask", json={"question": question}, headers=headers)

    resp = client.get("/support-chat/history", headers=headers)
    assert [m["question"] for m in resp.json()["messages"]] == ["First", "Second", "Third"]


def test_history_only_shows_callers_own_exchanges(auth_client):
    client, _ = auth_client
    alice = _register_and_login(client, "alice", "alice@example.com")
    bob = _register_and_login(client, "bob", "bob@example.com")
    client.post("/support-chat/ask", json={"question": "Alice's question"}, headers=alice)
    client.post("/support-chat/ask", json={"question": "Bob's question"}, headers=bob)

    alice_history = client.get("/support-chat/history", headers=alice).json()["messages"]
    bob_history = client.get("/support-chat/history", headers=bob).json()["messages"]
    assert [m["question"] for m in alice_history] == ["Alice's question"]
    assert [m["question"] for m in bob_history] == ["Bob's question"]
