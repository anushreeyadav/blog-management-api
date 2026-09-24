"""
Deterministic FAQ answers for the AI Support Chat -- no external API needed.

get_support_response(question) matches a question to one of FAQ_TOPICS by
keywords and returns that topic's answer, or FALLBACK_RESPONSE when nothing
matches. The same question always gets the same answer.

app/services/support_chat.py uses this module two ways: its topics are the
support knowledge given to Claude (when AI_CHAT_ENABLED is on), and its
answers are what users get whenever Claude is off or unavailable. It has no
database or FastAPI dependencies, so it can also be reused on its own.

Matching rules (see match_topic):
- A question and each topic's keywords are compared as lowercase words,
  with a trailing plural "s" removed ("posts" matches "post"). Everyday
  phrases that use "like" without meaning a post like ("I'd like to...",
  "looks like") are dropped first.
- A topic with requires_any only matches if the question contains at least
  one of those words, as written (so "likes" isn't satisfied by "like") -- e.g. "edit" only means edit_post when the question
  is about a post, so "change my plan" never lands there. A topic with
  excludes_any never matches a question containing one of those words --
  e.g. "post a comment" is about comments, not creating a post.
- The topic with the most keyword hits wins; ties go to the topic listed
  first in FAQ_TOPICS, so more specific topics are listed before general ones.
"""

import re
from dataclasses import dataclass, field

_WORD_RE = re.compile(r"[a-z0-9]+")

# "I'd like to...", "would like", "looks like", "feels like", "sounds like":
# ordinary English, not a question about liking posts.
_LIKE_FILLER_RE = re.compile(
    r"\b(?:would|i'?d|we'?d|you'?d|they'?d|he'?d|she'?d)\s+like\b|\b(?:looks?|feels?|sounds?|seems?)\s+like\b"
)


def _normalize(word: str) -> str:
    word = word.lower()
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _raw_words(text: str) -> frozenset[str]:
    text = _LIKE_FILLER_RE.sub(" ", text.lower().replace("\u2019", "'"))
    return frozenset(_WORD_RE.findall(text))


def _words(text: str) -> frozenset[str]:
    return frozenset(_normalize(word) for word in _raw_words(text))


@dataclass(frozen=True)
class FaqTopic:
    slug: str
    question: str  # a representative question, used when listing FAQs (e.g. for Claude's context)
    answer: str
    keywords: frozenset[str]
    requires_any: frozenset[str] = field(default_factory=frozenset)
    excludes_any: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        # Normalize once here so keywords can be written naturally ("posts", "Notifications").
        # requires_any stays as written: it is checked against the question's words as typed.
        for name in ("keywords", "excludes_any"):
            object.__setattr__(self, name, frozenset(_normalize(k) for k in getattr(self, name)))
        object.__setattr__(self, "requires_any", frozenset(k.lower() for k in self.requires_any))

    def score(self, question_words: frozenset[str], raw_words: frozenset[str] = frozenset()) -> int:
        if self.requires_any and not ((raw_words | question_words) & self.requires_any):
            return 0
        if question_words & self.excludes_any:
            return 0
        return len(question_words & self.keywords)


@dataclass(frozen=True)
class SupportResponse:
    topic: str | None  # FaqTopic.slug, or None when the fallback was used
    response: str

    @property
    def matched(self) -> bool:
        return self.topic is not None


_POST_WORDS = frozenset({"post", "posts", "blog", "article", "title", "content"})

FALLBACK_RESPONSE = (
    "I'm not sure about that yet. Please try asking about posts, comments, subscriptions, billing, "
    "your profile, dashboard analytics, or notifications."
)

# Order matters only for ties (see module docstring): specific before general.
#
# Answers describe what the user can do and where -- the assistant itself
# never performs actions (tests/test_support_faq.py checks for wording that
# would claim otherwise). Facts come from the code: plan limits from
# app/services/plans.py, subscription behaviour from app/routers/subscriptions.py.
FAQ_TOPICS: tuple[FaqTopic, ...] = (
    FaqTopic(
        slug="edit_post",
        question="How do I edit a post?",
        answer="To edit a post, send PUT /posts/{post_id} with the new title and/or content -- anything you "
        "leave out stays the same. The easiest way is the interactive API page at /docs. Only a post's "
        "author can edit it, and GET /posts/mine lists your posts and their ids.",
        keywords=frozenset({
            "edit", "editing", "edited", "update", "updating", "modify", "change", "changing",
            "rename", "fix", "typo", "correct",
        }),
        requires_any=_POST_WORDS,
    ),
    FaqTopic(
        slug="delete_post",
        question="How do I delete a post?",
        answer="To delete a post, send DELETE /posts/{post_id}, for example from the interactive API page at "
        "/docs. Deleting is permanent: the post's comments, likes and images go with it. Only the author can "
        "delete a post, and doing so frees up one post in your plan's limit.",
        keywords=frozenset({"delete", "deleting", "deleted", "remove", "removing", "erase", "undo", "unpublish"}),
        requires_any=_POST_WORDS,
    ),
    FaqTopic(
        slug="post_images",
        question="How do I add images to a post?",
        answer="To add an image to a post, upload it with POST /posts/{post_id}/image, for example from the "
        "interactive API page at /docs. How many images one post can hold depends on your plan: Basic 1, "
        "Premium 2, Pro unlimited.",
        keywords=frozenset({"image", "images", "photo", "picture", "gallery", "upload", "uploading", "attach"}),
    ),
    FaqTopic(
        slug="create_post",
        question="How do I create a post?",
        answer="To create a post, log in and send POST /posts with a title and content. The easiest way is "
        "the interactive API page at /docs. Your plan limits how many posts you can have (Basic 1, "
        "Premium 2, Pro unlimited); at the limit, creating another fails until you upgrade or delete an "
        "old post.",
        keywords=frozenset({
            "create", "creating", "new", "write", "writing", "publish", "publishing", "add", "make",
            "first", "start",
        }),
        requires_any=_POST_WORDS,
        # "Add a comment to a post" / "upload an image to my post" belong to those topics.
        excludes_any=frozenset({"comment", "like", "image", "photo", "picture", "gallery"}),
    ),
    FaqTopic(
        slug="comments",
        question="How do comments work?",
        answer="To comment, send POST /posts/{post_id}/comments with your text while logged in; anyone can "
        "read a post's comments with GET /posts/{post_id}/comments. Comments count toward your plan's limit "
        "(Basic 5, Premium 25, Pro unlimited), and the post's author is notified. Comments can't be edited "
        "or deleted yet.",
        keywords=frozenset({"comment", "comments", "commenting", "commented", "reply", "replies", "discussion"}),
    ),
    FaqTopic(
        slug="likes",
        question="How do likes work?",
        answer="To like a post, send POST /posts/{post_id}/like; DELETE /posts/{post_id}/like removes your "
        "like. You can like each post once. Likes count toward your plan's limit (Basic 5, Premium 25, Pro "
        "unlimited), and unliking frees that slot straight away. The post's author is notified.",
        keywords=frozenset({"like", "likes", "liking", "liked", "unlike", "unliking", "heart"}),
        # A bare "like" only counts when the question is about posts ("What's the weather like?" isn't).
        requires_any=_POST_WORDS | {"likes", "liking", "liked", "unlike", "unliking", "heart"},
    ),
    FaqTopic(
        slug="subscription_activation",
        question="How do I activate a subscription?",
        answer="To activate a plan, send POST /subscriptions/subscribe with its plan_id (GET /subscriptions/plans "
        "lists them), for example from /docs. It takes effect immediately: your new limits apply, an invoice "
        "is created, and you get a 'subscription activated' notification. A monthly plan runs for 30 days; "
        "GET /subscriptions/me shows your plan and its end date.",
        keywords=frozenset({
            "activate", "activating", "activated", "activation", "subscribe", "subscribing", "buy", "purchase",
            "join", "get", "start", "starting",
        }),
        requires_any=frozenset({
            "activate", "activating", "activated", "activation", "subscribe", "subscribing", "subscription",
            "plan", "basic", "premium", "pro", "buy", "purchase",
        }),
    ),
    FaqTopic(
        slug="subscription_renewal",
        question="How does subscription renewal work?",
        answer="Plans don't renew automatically yet. GET /subscriptions/me shows when your current period "
        "ends. Once it has ended, subscribe to the plan again (POST /subscriptions/subscribe) to start a new "
        "30-day period with a new invoice. Subscribing to the same plan while it's still active doesn't "
        "extend the end date.",
        keywords=frozenset({
            "renew", "renewal", "renewed", "renewing", "expire", "expired", "expiry", "expiration", "expiring",
            "extend", "end", "ends", "ending", "lapse", "lapsed", "automatically", "auto",
        }),
        # "My session expired" is about logging in, not the plan.
        excludes_any=frozenset({"session", "login", "token", "logged", "password"}),
    ),
    FaqTopic(
        slug="subscriptions",
        question="How do subscriptions work?",
        answer="There are three monthly plans: Basic (499/month: 1 post, 1 image per post, 5 likes, "
        "5 comments), Premium (999/month: 2 posts, 2 images per post, 25 likes, 25 comments) and Pro "
        "(1999/month: unlimited). New accounts start on Basic. Start a plan with POST /subscriptions/subscribe, "
        "switch with POST /subscriptions/change, "
        "cancel with POST /subscriptions/cancel (you go back to Basic), and see your plan and usage with "
        "GET /subscriptions/me and GET /subscriptions/usage. All of these work from /docs.",
        keywords=frozenset({
            "subscription", "subscriptions", "plan", "plans", "upgrade", "downgrade", "cancel", "switch",
            "basic", "premium", "pro", "tier", "limit", "limits", "quota", "usage",
        }),
    ),
    FaqTopic(
        slug="billing",
        question="How does billing work, and where are my invoices?",
        answer="Every plan purchase creates a billing record with a PDF invoice. List yours with "
        "GET /subscriptions/billing-history and download one with "
        "GET /subscriptions/billing/{billing_id}/invoice. For refunds or a charge you don't recognise, please "
        "contact an administrator -- support chat can't change billing.",
        keywords=frozenset({
            "billing", "bill", "invoice", "invoices", "payment", "pay", "paid", "paying", "charge",
            "charged", "receipt", "refund", "price", "pricing", "cost", "transaction", "pdf",
        }),
    ),
    FaqTopic(
        slug="notifications",
        question="How do notifications work?",
        answer="You're notified when someone likes or comments on your post, and when a subscription is "
        "activated or renewed. Click the bell icon on the dashboard to see them and mark them read, or use "
        "GET /notifications/, PATCH /notifications/{id}/read and PATCH /notifications/read-all.",
        keywords=frozenset({
            "notification", "notifications", "notify", "notified", "bell", "alert", "alerts", "unread",
        }),
    ),
    FaqTopic(
        slug="password_login",
        question="How do I log in, and what if I forget my password?",
        answer="Log in with POST /auth/login (username and password) to get an access token; on /docs, click "
        "Authorize and paste it. A login lasts about 30 minutes -- after that you'll see 'Could not validate "
        "credentials' and just need to log in again. Passwords must be at least 8 characters. There's no "
        "self-service password reset or change yet, so contact an administrator if you're locked out.",
        keywords=frozenset({
            "password", "passwords", "login", "log", "logged", "logout", "signin", "token", "jwt", "session",
            "expired", "unauthorized", "401", "credentials", "forgot", "forgotten", "reset", "locked",
        }),
    ),
    FaqTopic(
        slug="profile",
        question="How do I manage my profile?",
        answer="Your profile holds your username, email address and current plan. View it with GET /auth/me; "
        "your username also shows at the top of the dashboard. Changing your username or email, or deleting "
        "your account, isn't available yet -- please contact an administrator for any of those.",
        keywords=frozenset({
            "profile", "account", "username", "email", "name", "details", "personal", "info", "information",
        }),
    ),
    FaqTopic(
        slug="dashboard",
        question="What does the dashboard show?",
        answer="Your dashboard (/static/dashboard.html) shows analytics for your own posts: total posts, "
        "comments and likes received, views, a per-post breakdown and posts published per day, as charts. "
        "The same data is available from GET /dashboard/me. Only you can see your dashboard.",
        keywords=frozenset({
            "dashboard", "analytics", "analytic", "stats", "statistics", "view", "views", "chart", "charts",
            "graph", "metrics", "insights", "performance", "traffic", "report",
        }),
    ),
    FaqTopic(
        slug="general",
        question="How do I get started with the platform?",
        answer="This is a blogging platform: write posts with images, read and search other people's posts "
        "(GET /posts?search=...), comment and like, follow your posts' performance on the dashboard, and "
        "choose a subscription plan. To start, register with POST /auth/register and then log in. Most "
        "actions are done from the interactive API page at /docs. You can ask me about any of these topics.",
        keywords=frozenset({
            "platform", "about", "started", "begin", "beginning", "register", "registration", "signup", "sign",
            "search", "browse", "find", "help", "work", "works", "use", "using", "feature", "features",
        }),
    ),
)


def match_topic(question: str) -> FaqTopic | None:
    """The best-matching topic for `question`, or None if no topic matches at all."""
    raw_words = _raw_words(question)
    question_words = frozenset(_normalize(word) for word in raw_words)
    best_topic, best_score = None, 0
    for topic in FAQ_TOPICS:
        score = topic.score(question_words, raw_words)
        if score > best_score:
            best_topic, best_score = topic, score
    return best_topic


def get_support_response(question: str) -> SupportResponse:
    """The FAQ answer for `question`, or FALLBACK_RESPONSE when no topic matches."""
    topic = match_topic(question)
    if topic is None:
        return SupportResponse(topic=None, response=FALLBACK_RESPONSE)
    return SupportResponse(topic=topic.slug, response=topic.answer)
