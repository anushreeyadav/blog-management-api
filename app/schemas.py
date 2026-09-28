from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
)
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from app.models import POST_STATUS_DRAFT, POST_STATUS_PUBLISHED, POST_STATUS_SCHEDULED


def _require_non_blank(value: str, field_label: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_label} cannot be blank")
    return stripped


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class UserRegister(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v

    @field_validator("username")
    @classmethod
    def username_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "username")

    @field_validator("password")
    @classmethod
    def password_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("password cannot be blank")
        return v


class UserLogin(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @field_validator("username")
    @classmethod
    def username_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "username")

    @field_validator("password")
    @classmethod
    def password_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("password cannot be blank")
        return v


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr
    subscription_plan_id: int


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

# How a create/update request wants the post published. The post's stored
# status (app/models.py's Post.status) is derived from this -- clients never
# send status or published_at directly; published_at is recorded by the
# server when a post actually goes live.
PUBLISH_NOW = "publish_now"
SAVE_DRAFT = "save_draft"
SCHEDULE = "schedule"
PostPublishOption = Literal["publish_now", "save_draft", "schedule"]

PUBLISH_OPTION_TO_STATUS = {
    PUBLISH_NOW: POST_STATUS_PUBLISHED,
    SAVE_DRAFT: POST_STATUS_DRAFT,
    SCHEDULE: POST_STATUS_SCHEDULED,
}


# Validation messages for scheduled publishing. Raised as PydanticCustomError
# so the 422 "msg" is exactly this text (no "Value error, " prefix) and each
# case has its own stable "type" a client can branch on.
SCHEDULED_AT_ERRORS = {
    "scheduled_at_required": "scheduled_at is required when publish_option is 'schedule'. "
    "Choose a future date and time, e.g. 2026-10-01T09:00:00Z.",
    "scheduled_at_not_allowed": "scheduled_at can only be set when publish_option is 'schedule'. "
    "Remove it, or set publish_option to 'schedule'.",
    "scheduled_at_invalid_format": "scheduled_at must be a valid date and time, e.g. 2026-10-01T09:00:00Z.",
    "scheduled_at_missing_timezone": "scheduled_at must include a date, a time and a timezone, "
    "e.g. 2026-10-01T09:00:00Z or 2026-10-01T14:30:00+05:30.",
    "scheduled_at_not_in_future": "scheduled_at must be in the future. Choose a later date and time.",
    "status_read_only": "status can't be set directly. Use publish_option instead: "
    "'publish_now', 'save_draft' or 'schedule'.",
    "published_at_read_only": "published_at can't be set directly. "
    "It's recorded automatically when the post goes live.",
}


def _publishing_error(error_type: str) -> PydanticCustomError:
    return PydanticCustomError(error_type, SCHEDULED_AT_ERRORS[error_type])


def _validate_scheduled_at(
    value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
) -> datetime | None:
    """
    Shared by PostCreate and PostUpdate (a wrap validator, so malformed input
    gets the friendly format message instead of the parser's). publish_option
    is declared before scheduled_at on both, so it's already in info.data
    here -- unless it was itself invalid, in which case that error is the one
    reported.

    All comparisons are timezone-aware, in UTC -- the convention every stored
    timestamp here follows (timestamptz columns; created_at/published_at from
    the database clock, scheduled_at normalized below).
    """
    try:
        value = handler(value)
    except ValidationError:
        raise _publishing_error("scheduled_at_invalid_format")

    if "publish_option" not in info.data:
        return value
    option = info.data["publish_option"]

    if option != SCHEDULE:
        if value is not None:
            raise _publishing_error("scheduled_at_not_allowed")
        return value

    if value is None:
        raise _publishing_error("scheduled_at_required")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        # Also covers a bare date ("2026-10-01"), which parses as naive midnight.
        raise _publishing_error("scheduled_at_missing_timezone")
    if value <= datetime.now(timezone.utc):
        raise _publishing_error("scheduled_at_not_in_future")
    # Normalized to UTC: same instant, but SQLite drops the offset on
    # storage, so a "+05:30" time would otherwise be read back as UTC.
    return value.astimezone(timezone.utc)


def _reject_server_managed_field(value: Any, info: ValidationInfo) -> None:
    """status/published_at are server-controlled. Sending one used to be
    silently ignored, which let a client believe it had set it; an explicit
    null is still accepted as "not set"."""
    if value is not None:
        raise _publishing_error(f"{info.field_name}_read_only")
    return None


class PostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    # Defaults to publish_now -- the only behavior POST /posts had before
    # scheduled publishing existed, so existing clients are unaffected.
    publish_option: PostPublishOption = PUBLISH_NOW
    # Required (and only allowed) with publish_option="schedule"; must be
    # timezone-aware and in the future. validate_default so a missing value
    # is still checked against publish_option.
    scheduled_at: datetime | None = Field(default=None, validate_default=True)
    # Declared only to reject them with a clear message (see
    # _reject_server_managed_field); hidden from the OpenAPI schema.
    status: SkipJsonSchema[Any] = Field(default=None, exclude=True)
    published_at: SkipJsonSchema[Any] = Field(default=None, exclude=True)

    @property
    def target_status(self) -> str:
        return PUBLISH_OPTION_TO_STATUS[self.publish_option]

    @field_validator("scheduled_at", mode="wrap")
    @classmethod
    def scheduled_at_matches_publish_option(
        cls, v: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> datetime | None:
        return _validate_scheduled_at(v, handler, info)

    @field_validator("status", "published_at", mode="before")
    @classmethod
    def server_managed_fields_rejected(cls, v: Any, info: ValidationInfo) -> None:
        return _reject_server_managed_field(v, info)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "title")

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "content")


class PostUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    content: str | None = None
    # None (the default) leaves the post's current status untouched, so
    # existing title/content-only edits behave exactly as before.
    publish_option: PostPublishOption | None = None
    scheduled_at: datetime | None = Field(default=None, validate_default=True)
    status: SkipJsonSchema[Any] = Field(default=None, exclude=True)
    published_at: SkipJsonSchema[Any] = Field(default=None, exclude=True)

    @property
    def target_status(self) -> str | None:
        return PUBLISH_OPTION_TO_STATUS[self.publish_option] if self.publish_option else None

    @field_validator("scheduled_at", mode="wrap")
    @classmethod
    def scheduled_at_matches_publish_option(
        cls, v: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> datetime | None:
        return _validate_scheduled_at(v, handler, info)

    @field_validator("status", "published_at", mode="before")
    @classmethod
    def server_managed_fields_rejected(cls, v: Any, info: ValidationInfo) -> None:
        return _reject_server_managed_field(v, info)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _require_non_blank(v, "title")

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _require_non_blank(v, "content")


class PostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    content: str
    author_id: int
    created_at: datetime
    image: str | None = None  # kept for existing consumers: the most recently uploaded image
    images: list[str] = []  # the full gallery, in upload order -- may hold more than one for Premium/Pro
    status: str = POST_STATUS_PUBLISHED  # draft / scheduled / published
    scheduled_at: datetime | None = None  # when a scheduled post goes live
    published_at: datetime | None = None  # when the post actually went live; None until then

    @field_validator("scheduled_at", "published_at")
    @classmethod
    def _publishing_times_are_utc_aware(cls, v: datetime | None) -> datetime | None:
        # Stored as UTC. PostgreSQL already returns aware values; SQLite
        # returns naive ones, which clients would otherwise read as local time.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("images", mode="before")
    @classmethod
    def _extract_image_urls(cls, v):
        if not v:
            return []
        return [item.image if hasattr(item, "image") else item for item in v]


class PaginatedPosts(BaseModel):
    items: list[PostResponse]
    page: int
    limit: int
    total: int
    total_pages: int


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class CommentCreate(BaseModel):
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "comment text")


class CommentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    post_id: int
    user_id: int
    text: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Likes
# ---------------------------------------------------------------------------

class LikeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    post_id: int
    user_id: int


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

class SubscriptionPlanResponse(BaseModel):
    """One subscription plan (Basic, Premium, or Pro). A `null` limit
    field (max_posts/max_images/max_likes/max_comments) means unlimited --
    Pro's plans always report null for all four."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    price: float
    billing_interval: str
    max_posts: int | None
    max_images: int | None
    max_likes: int | None
    max_comments: int | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class SubscriptionPlansResponse(BaseModel):
    """GET /subscriptions/plans -- every currently active plan, cheapest first."""

    plans: list[SubscriptionPlanResponse]


class SubscribeRequest(BaseModel):
    """POST /subscriptions/subscribe's request body -- plan_id is the
    *only* accepted field. A client-supplied "price", "max_posts",
    "status", etc. is rejected outright (422) rather than being silently
    discarded: the plan's real name, price, and limits always come from
    the SubscriptionPlan row looked up server-side by plan_id (see
    app/routers/subscriptions.py), never from anything in this body."""

    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(
        ...,
        description="ID of the subscription plan to subscribe to. See GET /subscriptions/plans for valid ids "
        "(e.g. the Basic/Premium/Pro plans' own `id` fields). No other field is accepted -- the plan's real "
        "name, price, and limits are always looked up server-side from this id, never taken from the request.",
        examples=[2],
    )


class SubscriptionChangeRequest(SubscribeRequest):
    """POST /subscriptions/change's request body. Identical shape to
    SubscribeRequest (just plan_id, extra fields forbidden) -- changing
    plans is the same operation as subscribing (see _change_subscription
    in app/routers/subscriptions.py), so this inherits rather than
    duplicates that validation. Kept as its own named class since the two
    endpoints are conceptually distinct requests."""


class SubscriptionResponse(BaseModel):
    """A single Subscription record -- the raw row backing a user's
    subscribe/change/cancel actions (POST /subscriptions/subscribe,
    /change, /cancel all return or embed one of these)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    plan_id: int
    status: str
    current_period_start: datetime
    current_period_end: datetime
    auto_renew: bool
    canceled_at: datetime | None = None


class CurrentSubscriptionResponse(BaseModel):
    """GET /subscriptions/me -- always resolvable, since every user always
    has an effective plan (Basic by default; see app/models.py). The
    subscription dates are only populated for a plan the user actually
    subscribed to; a user who has never called /subscribe has no formal
    Subscription row, so those come back null rather than 404ing."""

    user_id: int
    plan: "CurrentSubscriptionPlanResponse"
    start_date: datetime | None = None
    end_date: datetime | None = None
    status: str  # the Subscription row's status, or "default" if none exists


class CurrentSubscriptionPlanResponse(BaseModel):
    id: int
    name: str
    price: float
    max_posts: int | None
    max_images: int | None
    max_likes: int | None
    max_comments: int | None


class UsageMetric(BaseModel):
    used: int
    limit: int | None  # None = unlimited (Pro)
    remaining: int | None  # None = unlimited (Pro)


class SubscriptionUsageResponse(BaseModel):
    """GET /subscriptions/usage. images.used is a total across all of the
    user's posts, while images.limit is applied per post at upload time
    (see app/services/subscription.py) -- the two are different scopes by
    necessity, since there's no single "the" post to report against in a
    per-user summary."""

    plan: str | None
    usage: dict[str, UsageMetric]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardMetricsResponse(BaseModel):
    """
    The authenticated user's own metrics only (see
    app/services/dashboard_service.py). All four fields are "about my
    content": total_comments_received and total_likes_received both count
    engagement received on the user's own posts (not, e.g., comments the
    user wrote on someone else's post). total_views reflects
    Post.view_count, a running total incremented on every GET
    /posts/{id} (see app/routers/posts.py); it is not a deduplicated
    unique-visitor count.
    """

    total_posts: int
    total_comments_received: int
    total_likes_received: int
    total_views: int


class DashboardUserInfo(BaseModel):
    """
    Deliberately minimal -- just enough to label whose dashboard this is.
    Not UserResponse: this endpoint never exposes email,
    subscription_plan_id, or any other field beyond id/username, per
    GET /dashboard/me's "no unrelated private info" requirement.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str


class PostAnalyticsItem(BaseModel):
    """
    One post's like/comment/view counts, for frontend charts (see
    app/services/dashboard_service.py's get_post_analytics). views is
    always present -- Post.view_count is tracked for every post (see
    app/models.py) -- rather than an optional/nullable field for a
    tracking feature that might not exist.
    """

    post_id: int
    title: str
    likes: int
    comments: int
    views: int


class PostActivityPoint(BaseModel):
    """
    One calendar day's post-creation count, for a frontend line chart (see
    app/services/dashboard_service.py's get_post_activity). `date` is
    emitted as YYYY-MM-DD regardless of whether the database driver
    returned a string (SQLite) or a native date object (PostgreSQL) --
    Pydantic normalizes either into this field's date type.
    """

    date: date
    posts: int


class DashboardResponse(BaseModel):
    """
    GET /dashboard/me -- the authenticated user's own info, statistics, and
    per-post breakdown only. There is no user_id anywhere in this path or
    body, so another user's dashboard -- including their post_analytics and
    post_activity -- can never be requested through this endpoint.
    """

    user: DashboardUserInfo
    statistics: DashboardMetricsResponse
    post_analytics: list[PostAnalyticsItem]
    post_activity: list[PostActivityPoint]


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

class BillingHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    subscription_plan_id: int
    transaction_id: str
    amount: float
    start_date: datetime
    end_date: datetime
    status: str
    invoice_path: str | None = None
    created_at: datetime


class BillingHistoryItem(BaseModel):
    """One row of GET /subscriptions/billing-history -- the user-facing
    shape (plan name, invoice_url) rather than BillingHistoryResponse's
    raw/internal shape (plan_id, invoice_path, user_id) used by the
    admin-only cross-user listing in app/routers/admin.py. Built from a
    plain dict in the router rather than from_attributes, since `plan` and
    `invoice_url` don't map 1:1 onto the BillingHistory model's own
    attribute names (`plan.name`, `invoice_path`)."""

    id: int
    plan: str
    transaction_id: str
    amount: float
    start_date: datetime
    end_date: datetime
    status: str
    invoice_url: str | None = None
    created_at: datetime


class BillingHistoryListResponse(BaseModel):
    """Paginated the same way GET /posts already is (page/limit/total/
    total_pages) -- this app's one established pagination convention."""

    billing_history: list[BillingHistoryItem]
    page: int
    limit: int
    total: int
    total_pages: int


class SubscribeResponse(BaseModel):
    """POST /subscriptions/subscribe -- both the new subscription and the
    invoice/billing record it generated, so a caller never needs a second
    request just to see what they were charged."""

    subscription: SubscriptionResponse
    invoice: BillingHistoryResponse


class SubscriptionChangeDetails(BaseModel):
    plan: str
    start_date: datetime
    end_date: datetime
    status: str


class SubscriptionChangeBilling(BaseModel):
    transaction_id: str
    amount: float
    invoice_url: str


class SubscriptionChangeResponse(BaseModel):
    message: str
    subscription: SubscriptionChangeDetails
    billing: SubscriptionChangeBilling


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message: str
    notification_type: str
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    """GET /notifications/ -- the caller's own notifications, newest first,
    plus unread_count so a client can show a badge without a second
    request or counting the page itself (unread_count is a total over all
    of the user's notifications, not just the ones returned here)."""

    notifications: list[NotificationResponse]
    unread_count: int


class NotificationMarkAllReadResponse(BaseModel):
    """PATCH /notifications/read-all."""

    message: str
    updated_count: int


# ---------------------------------------------------------------------------
# AI Support Chat
# ---------------------------------------------------------------------------

SUPPORT_CHAT_QUESTION_MAX_LENGTH = 2000


class SupportChatAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=SUPPORT_CHAT_QUESTION_MAX_LENGTH)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "question")


class SupportChatMessageResponse(BaseModel):
    """One stored exchange. response_source is "claude" or "predefined"
    (the FAQ fallback used when the AI is unavailable)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    question: str
    response: str
    response_source: str
    created_at: datetime


class SupportChatHistoryResponse(BaseModel):
    """GET /support-chat/history -- the caller's own exchanges, oldest
    first so a chat window can render them top to bottom as-is."""

    messages: list[SupportChatMessageResponse]


class AiSupportRequest(BaseModel):
    """POST /api/ai-support/ -- same limits as SupportChatAskRequest."""

    message: str = Field(
        min_length=1,
        max_length=SUPPORT_CHAT_QUESTION_MAX_LENGTH,
        examples=["How do I create a post?"],
    )

    @field_validator("message")
    @classmethod
    def message_not_blank(cls, v: str) -> str:
        return _require_non_blank(v, "message")


class AiSupportResponse(BaseModel):
    """response is the answer shown to the user; timestamp is when the
    exchange was saved (the stored SupportChatMessage.created_at)."""

    response: str
    timestamp: datetime


class AiSupportHistoryItem(BaseModel):
    """One saved exchange. ai_response is SupportChatMessage.response."""

    id: int
    question: str
    ai_response: str
    created_at: datetime


class AiSupportHistoryResponse(BaseModel):
    """GET /api/ai-support/history/ -- the caller's own exchanges, newest
    first, paginated the same way as GET /posts (page/limit/total/total_pages)."""

    messages: list[AiSupportHistoryItem]
    page: int
    limit: int
    total: int
    total_pages: int
