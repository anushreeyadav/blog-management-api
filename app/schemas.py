from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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

class PostCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)

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


class SubscribeRequest(BaseModel):
    # Only plan_id is ever accepted -- rejects a client-supplied "price",
    # "max_posts", "status", etc. outright, rather than relying on it
    # being silently discarded. The plan's real limits always come from
    # the SubscriptionPlan row looked up server-side by plan_id (see
    # app/routers/subscriptions.py), never from anything in this body.
    model_config = ConfigDict(extra="forbid")

    plan_id: int


class SubscriptionResponse(BaseModel):
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

    plan_id: int
    plan_name: str
    price: float
    max_posts: int | None
    max_images: int | None
    max_likes: int | None
    max_comments: int | None
    subscription_status: str  # the Subscription row's status, or "default" if none exists
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None


class UsageMetric(BaseModel):
    used: int
    limit: int | None  # None = unlimited (Pro)


class SubscriptionUsageResponse(BaseModel):
    """GET /subscriptions/usage. images.used is a total across all of the
    user's posts, while images.limit is applied per post at upload time
    (see app/services/subscription.py) -- the two are different scopes by
    necessity, since there's no single "the" post to report against in a
    per-user summary."""

    plan: str
    posts: UsageMetric
    images: UsageMetric
    likes: UsageMetric
    comments: UsageMetric


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


class SubscribeResponse(BaseModel):
    """POST /subscriptions/subscribe -- both the new subscription and the
    invoice/billing record it generated, so a caller never needs a second
    request just to see what they were charged."""

    subscription: SubscriptionResponse
    invoice: BillingHistoryResponse
