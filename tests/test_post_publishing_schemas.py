"""
Request/response schemas for scheduled publishing (app/schemas.py's
PostCreate / PostUpdate publish_option + scheduled_at, and PostResponse's
status / scheduled_at / published_at). Schema-level only -- like
test_api.py's TestAPILevelValidation, the 422 checks below use a tiny
standalone app and never touch app.main, app.routers or a database.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas import SCHEDULED_AT_ERRORS, PostCreate, PostResponse, PostUpdate


def _future(**delta) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**(delta or {"days": 1}))


def _past(**delta) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**(delta or {"days": 1}))


def _errors_for(exc: ValidationError, field: str) -> list[str]:
    return [e["msg"] for e in exc.errors() if e["loc"] == (field,)]


# ---------------------------------------------------------------------------
# PostCreate
# ---------------------------------------------------------------------------


class TestPostCreatePublishOptions:
    def test_defaults_to_publish_now_like_before(self):
        post = PostCreate(title="t", content="c")
        assert post.publish_option == "publish_now"
        assert post.scheduled_at is None
        assert post.target_status == "published"

    def test_publish_now(self):
        post = PostCreate(title="t", content="c", publish_option="publish_now")
        assert post.target_status == "published"
        assert post.scheduled_at is None

    def test_save_draft(self):
        post = PostCreate(title="t", content="c", publish_option="save_draft")
        assert post.target_status == "draft"
        assert post.scheduled_at is None

    def test_schedule_with_future_time(self):
        when = _future()
        post = PostCreate(title="t", content="c", publish_option="schedule", scheduled_at=when)
        assert post.target_status == "scheduled"
        assert post.scheduled_at == when

    def test_schedule_accepts_iso_string_with_offset_and_normalizes_to_utc(self):
        when = _future()
        ist = when.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
        post = PostCreate(title="t", content="c", publish_option="schedule", scheduled_at=ist)
        assert post.scheduled_at == when
        assert post.scheduled_at.utcoffset() == timedelta(0)

    def test_schedule_without_scheduled_at_rejected(self):
        with pytest.raises(ValidationError) as exc:
            PostCreate(title="t", content="c", publish_option="schedule")
        assert _errors_for(exc.value, "scheduled_at") == [SCHEDULED_AT_ERRORS["scheduled_at_required"]]

    def test_schedule_in_the_past_rejected(self):
        with pytest.raises(ValidationError) as exc:
            PostCreate(title="t", content="c", publish_option="schedule", scheduled_at=_past())
        assert _errors_for(exc.value, "scheduled_at") == [SCHEDULED_AT_ERRORS["scheduled_at_not_in_future"]]

    def test_schedule_right_now_rejected(self):
        with pytest.raises(ValidationError):
            PostCreate(title="t", content="c", publish_option="schedule", scheduled_at=datetime.now(timezone.utc))

    def test_schedule_without_timezone_rejected(self):
        naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
        with pytest.raises(ValidationError) as exc:
            PostCreate(title="t", content="c", publish_option="schedule", scheduled_at=naive)
        assert _errors_for(exc.value, "scheduled_at") == [SCHEDULED_AT_ERRORS["scheduled_at_missing_timezone"]]

    @pytest.mark.parametrize("option", ["publish_now", "save_draft"])
    def test_scheduled_at_rejected_unless_scheduling(self, option):
        with pytest.raises(ValidationError) as exc:
            PostCreate(title="t", content="c", publish_option=option, scheduled_at=_future())
        assert _errors_for(exc.value, "scheduled_at") == [SCHEDULED_AT_ERRORS["scheduled_at_not_allowed"]]

    def test_scheduled_at_without_publish_option_rejected(self):
        # publish_option defaults to publish_now, so a bare scheduled_at is a mismatch.
        with pytest.raises(ValidationError):
            PostCreate(title="t", content="c", scheduled_at=_future())

    @pytest.mark.parametrize("option", ["publish", "draft", "scheduled", "PUBLISH_NOW", ""])
    def test_unknown_publish_option_rejected(self, option):
        with pytest.raises(ValidationError) as exc:
            PostCreate(title="t", content="c", publish_option=option)
        assert [e["loc"] for e in exc.value.errors()] == [("publish_option",)]

    @pytest.mark.parametrize("field,value", [("status", "published"), ("published_at", "2020-01-01T00:00:00Z")])
    def test_client_cannot_set_status_or_published_at_directly(self, field, value):
        with pytest.raises(ValidationError) as exc:
            PostCreate(title="t", content="c", **{field: value})
        assert _errors_for(exc.value, field) == [SCHEDULED_AT_ERRORS[f"{field}_read_only"]]

    def test_explicit_null_status_and_published_at_still_accepted(self):
        post = PostCreate(title="t", content="c", status=None, published_at=None)
        assert post.target_status == "published"
        assert "status" not in post.model_dump() and "published_at" not in post.model_dump()

    def test_existing_title_content_validation_unchanged(self):
        with pytest.raises(ValidationError):
            PostCreate(title="   ", content="c", publish_option="save_draft")


# ---------------------------------------------------------------------------
# PostUpdate
# ---------------------------------------------------------------------------


class TestPostUpdatePublishOptions:
    def test_no_publish_option_leaves_status_unchanged(self):
        update = PostUpdate(title="New")
        assert update.publish_option is None
        assert update.scheduled_at is None
        assert update.target_status is None

    def test_empty_update_still_valid(self):
        update = PostUpdate()
        assert update.target_status is None

    @pytest.mark.parametrize(
        "option,status", [("publish_now", "published"), ("save_draft", "draft")]
    )
    def test_publish_now_and_save_draft(self, option, status):
        update = PostUpdate(publish_option=option)
        assert update.target_status == status

    def test_schedule_with_future_time(self):
        update = PostUpdate(publish_option="schedule", scheduled_at=_future(hours=2))
        assert update.target_status == "scheduled"

    def test_schedule_without_scheduled_at_rejected(self):
        with pytest.raises(ValidationError) as exc:
            PostUpdate(publish_option="schedule")
        assert _errors_for(exc.value, "scheduled_at") == [SCHEDULED_AT_ERRORS["scheduled_at_required"]]

    def test_schedule_in_the_past_rejected(self):
        with pytest.raises(ValidationError):
            PostUpdate(publish_option="schedule", scheduled_at=_past(minutes=1))

    def test_scheduled_at_without_publish_option_rejected(self):
        with pytest.raises(ValidationError) as exc:
            PostUpdate(scheduled_at=_future())
        assert _errors_for(exc.value, "scheduled_at") == [SCHEDULED_AT_ERRORS["scheduled_at_not_allowed"]]

    def test_invalid_publish_option_reports_only_that_error(self):
        with pytest.raises(ValidationError) as exc:
            PostUpdate(publish_option="later", scheduled_at=_future())
        assert [e["loc"] for e in exc.value.errors()] == [("publish_option",)]


# ---------------------------------------------------------------------------
# PostResponse
# ---------------------------------------------------------------------------


def _post_obj(**overrides):
    base = dict(
        id=1, title="t", content="c", author_id=1, created_at=datetime.now(timezone.utc),
        image=None, images=[], status="published", scheduled_at=None, published_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPostResponsePublishingFields:
    def test_existing_fields_still_present(self):
        body = PostResponse.model_validate(_post_obj()).model_dump()
        assert {"id", "title", "content", "author_id", "created_at", "image", "images"}.issubset(body)

    def test_includes_publishing_fields(self):
        when = _future()
        published = _past()
        body = PostResponse.model_validate(
            _post_obj(status="scheduled", scheduled_at=when, published_at=published)
        ).model_dump()
        assert body["status"] == "scheduled"
        assert body["scheduled_at"] == when
        assert body["published_at"] == published

    def test_defaults_when_built_from_a_dict_without_them(self):
        body = PostResponse(
            id=1, title="t", content="c", author_id=1, created_at=datetime.now(timezone.utc)
        ).model_dump()
        assert body["status"] == "published"
        assert body["scheduled_at"] is None
        assert body["published_at"] is None


# ---------------------------------------------------------------------------
# 422 responses over HTTP
# ---------------------------------------------------------------------------

schema_app = FastAPI()


@schema_app.post("/posts")
def _create(payload: PostCreate):
    return {"status": payload.target_status}


@schema_app.put("/posts")
def _update(payload: PostUpdate):
    return {"status": payload.target_status}


http = TestClient(schema_app)


class TestPublishingValidationOverHTTP:
    def test_old_style_create_body_still_accepted(self):
        resp = http.post("/posts", json={"title": "t", "content": "c"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "published"}

    def test_schedule_accepted(self):
        resp = http.post(
            "/posts",
            json={"title": "t", "content": "c", "publish_option": "schedule", "scheduled_at": _future().isoformat()},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "scheduled"}

    def test_schedule_missing_time_is_a_clear_422(self):
        resp = http.post("/posts", json={"title": "t", "content": "c", "publish_option": "schedule"})
        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "scheduled_at"]
        assert error["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_required"]

    def test_past_schedule_is_a_clear_422(self):
        resp = http.put("/posts", json={"publish_option": "schedule", "scheduled_at": _past().isoformat()})
        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "scheduled_at"]
        assert error["msg"] == SCHEDULED_AT_ERRORS["scheduled_at_not_in_future"]

    def test_unknown_option_lists_allowed_values(self):
        resp = http.post("/posts", json={"title": "t", "content": "c", "publish_option": "later"})
        assert resp.status_code == 422
        [error] = resp.json()["detail"]
        assert error["loc"] == ["body", "publish_option"]
        assert "'publish_now', 'save_draft' or 'schedule'" in error["msg"]

    def test_malformed_datetime_is_a_422(self):
        resp = http.post(
            "/posts",
            json={"title": "t", "content": "c", "publish_option": "schedule", "scheduled_at": "tomorrow"},
        )
        assert resp.status_code == 422
