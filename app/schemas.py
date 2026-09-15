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
    image: str | None = None


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
