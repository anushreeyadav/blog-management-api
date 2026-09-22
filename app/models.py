from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Every user has exactly one active plan at a time -- defaults to Basic
    # (see app/services/subscription.py) and is kept in sync whenever the
    # user subscribes/cancels. This is separate from Subscription (the
    # historical record of each subscribe/cancel event) and BillingHistory
    # (the historical invoices), neither of which this field touches.
    subscription_plan_id: Mapped[int] = mapped_column(
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    posts: Mapped[list["Post"]] = relationship(
        back_populates="author", cascade="all, delete-orphan", passive_deletes=True
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    likes: Mapped[list["Like"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    billing_history: Mapped[list["BillingHistory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    subscription_plan: Mapped["SubscriptionPlan"] = relationship(back_populates="users")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Indexed -- every dashboard query (app/services/dashboard_service.py)
    # and GET /posts/mine filters on this column; without an index each
    # becomes a full table scan of posts as the table grows.
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Incremented once per GET /posts/{id} request (see app/routers/posts.py).
    # A simple running total, not a per-viewer log -- deliberately NOT
    # deduplicated by visitor/session/IP: a page refresh, or the same
    # caller fetching the post twice, increments this again each time.
    # This matches how max_likes/max_comments already count live totals
    # rather than deduplicated histories elsewhere in this app, and avoids
    # the real complexity a unique-view counter would add (a session/IP
    # log, a time window, etc.) for a metric this project doesn't need to
    # be that precise about. GET /posts/{id} is public/unauthenticated, so
    # this also counts the author's own views of their own post -- there
    # is no reliable "is this the owner" check available on an anonymous
    # request without adding auth to a route that's deliberately public.
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    author: Mapped["User"] = relationship(back_populates="posts")
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )
    likes: Mapped[list["Like"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )
    images: Mapped[list["PostImage"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True, order_by="PostImage.id"
    )


class PostImage(Base):
    """
    A post's image gallery. Post.image (singular) is kept as-is for
    backward compatibility -- existing API consumers reading that one field
    keep working unchanged, always seeing the most recently uploaded image
    -- while this table is the real source of truth for how many images a
    post has, letting Premium/Pro plans hold more than one.
    """

    __tablename__ = "post_images"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    image: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    post: Mapped["Post"] = relationship(back_populates="images")


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Indexed -- GET /posts/{id}/comments (app/routers/comments.py) and the
    # dashboard (app/services/dashboard_service.py's get_post_analytics and
    # get_total_comments_received) both group/filter/join comments by
    # post_id.
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True)
    # Indexed -- the subscription plan's comment-limit check
    # (app/services/subscription.py's _current_usage) filters comments by
    # user_id.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    post: Mapped["Post"] = relationship(back_populates="comments")
    user: Mapped["User"] = relationship(back_populates="comments")


class Like(Base):
    __tablename__ = "likes"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_likes_post_id_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    post: Mapped["Post"] = relationship(back_populates="likes")
    user: Mapped["User"] = relationship(back_populates="likes")


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    billing_interval: Mapped[str] = mapped_column(String(10), nullable=False)  # "month" or "year"
    # Configurable per-plan limits. NULL means unlimited (the same convention
    # max_posts already used) rather than an arbitrary large sentinel value,
    # so plan behavior never has to be hard-coded per feature elsewhere.
    max_posts: Mapped[int | None] = mapped_column(nullable=True)
    max_images: Mapped[int | None] = mapped_column(nullable=True)
    max_likes: Mapped[int | None] = mapped_column(nullable=True)
    max_comments: Mapped[int | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="plan")
    billing_history: Mapped[list["BillingHistory"]] = relationship(back_populates="plan")
    users: Mapped[list["User"]] = relationship(back_populates="subscription_plan")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # active / canceled / expired
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    plan: Mapped["SubscriptionPlan"] = relationship(back_populates="subscriptions")


class BillingHistory(Base):
    """
    One immutable record per subscription purchase/renewal -- a receipt, not
    a live view of a Subscription. It links directly to the User and the
    SubscriptionPlan (not to the mutable Subscription row) so a billing
    record -- and the invoice it documents -- stays intact and correct even
    after the subscription it paid for is later canceled or changed.
    """

    __tablename__ = "billing_history"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_plan_id: Mapped[int] = mapped_column(
        ForeignKey("subscription_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    transaction_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # paid / failed / refunded
    invoice_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="billing_history")
    plan: Mapped["SubscriptionPlan"] = relationship(back_populates="billing_history")
