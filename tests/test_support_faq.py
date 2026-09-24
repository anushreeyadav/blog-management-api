"""
Unit tests for app/services/support_faq.py -- the deterministic, FAQ-based
support answers. Pure functions: no database, HTTP client or network.
"""

import ast

import pytest

from app.services import support_faq
from app.services.support_faq import FALLBACK_RESPONSE, FAQ_TOPICS, SupportResponse, get_support_response, match_topic

REQUIRED_TOPICS = {
    "create_post",
    "edit_post",
    "delete_post",
    "comments",
    "likes",
    "subscriptions",
    "subscription_activation",
    "subscription_renewal",
    "billing",
    "profile",
    "dashboard",
    "notifications",
    "password_login",
    "general",
}


def _answer_for(slug: str) -> str:
    return next(topic.answer for topic in FAQ_TOPICS if topic.slug == slug)


# ---------------------------------------------------------------------------
# Topic catalogue
# ---------------------------------------------------------------------------

def test_all_required_topics_exist():
    assert REQUIRED_TOPICS <= {topic.slug for topic in FAQ_TOPICS}


@pytest.mark.parametrize("topic", FAQ_TOPICS, ids=lambda t: t.slug)
def test_answers_never_claim_the_assistant_performed_an_action(topic):
    # Support answers explain what the user can do; the assistant itself
    # can't change posts, plans, billing or accounts.
    lowered = topic.answer.lower()
    for claim in (
        "i've ", "i have ", "i will ", "i'll ", "i've", "i can do", "i can reset", "i can change",
        "i reset", "i updated", "i changed", "i cancelled", "i canceled", "i refunded", "i deleted",
        "has been updated", "has been reset", "has been cancel", "has been refunded", "has been deleted",
        "done for you", "on your behalf",
    ):
        assert claim not in lowered, f"{topic.slug}: {claim!r}"


@pytest.mark.parametrize("topic", FAQ_TOPICS, ids=lambda t: t.slug)
def test_answers_stay_concise(topic):
    assert len(topic.answer) <= 500, f"{topic.slug} answer is {len(topic.answer)} characters"


@pytest.mark.parametrize("topic", FAQ_TOPICS, ids=lambda t: t.slug)
def test_answers_do_not_describe_screens_the_app_does_not_have(topic):
    # The only page is the dashboard; posts, comments and subscriptions are
    # managed through the API (e.g. /docs), so answers must not point users
    # at "Create Post"/"Edit" options or an account section that don't exist.
    for phrase in (" option", "section", "Open your post", "Open any post"):
        assert phrase not in topic.answer


def test_topic_slugs_are_unique():
    slugs = [topic.slug for topic in FAQ_TOPICS]
    assert len(slugs) == len(set(slugs))


@pytest.mark.parametrize("topic", FAQ_TOPICS, ids=lambda t: t.slug)
def test_every_topic_is_complete(topic):
    assert topic.question.strip()
    assert topic.answer.strip()
    assert topic.keywords


@pytest.mark.parametrize("topic", FAQ_TOPICS, ids=lambda t: t.slug)
def test_every_topic_answers_its_own_representative_question(topic):
    assert match_topic(topic.question) is topic


# ---------------------------------------------------------------------------
# The examples from the feature request
# ---------------------------------------------------------------------------

def test_create_post_example():
    result = get_support_response("How do I create a post?")
    assert result.topic == "create_post"
    assert result.response.startswith("To create a post, log in and send POST /posts")


def test_edit_post_example():
    result = get_support_response("How can I edit my post?")
    assert result.topic == "edit_post"
    assert result.response.startswith("To edit a post, send PUT /posts/{post_id}")


def test_subscriptions_example():
    result = get_support_response("How do subscriptions work?")
    assert result.topic == "subscriptions"
    assert "POST /subscriptions/change" in result.response and "POST /subscriptions/cancel" in result.response


# ---------------------------------------------------------------------------
# Topic matching
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question, expected_topic",
    [
        # Creating a post
        ("How do I write a new blog post?", "create_post"),
        ("Where do I publish my first article?", "create_post"),
        ("I can't create a post, it says I've reached my limit", "create_post"),
        # Editing a post
        ("How do I change the title of my post?", "edit_post"),
        ("I made a typo in my post, can I fix it?", "edit_post"),
        ("Can I update the content of an article?", "edit_post"),
        # Deleting a post
        ("How do I delete a post?", "delete_post"),
        ("Can I remove an old blog post?", "delete_post"),
        ("Does deleting a post remove its comments?", "delete_post"),
        # Post images
        ("How do I upload a photo to my post?", "post_images"),
        # Comments and likes
        # Comments
        ("How do I add a comment to a post?", "comments"),
        ("How do I reply to a comment?", "comments"),
        ("Can I delete a comment?", "comments"),
        # Likes
        ("How can I unlike something?", "likes"),
        ("How do likes work?", "likes"),
        ("Why can't I like this post twice?", "likes"),
        ("How do I like a post?", "likes"),
        ("Can I add a like to a post?", "likes"),
        # "like" used as ordinary English must not mean likes
        ("I'd like to write a post", "create_post"),
        ("I\u2019d like to write a post", "create_post"),
        ("I would like to cancel my subscription", "subscriptions"),
        ("I'd like to see my invoices", "billing"),
        ("What does the dashboard look like?", "dashboard"),
        # Subscriptions
        ("What plans do you offer?", "subscriptions"),
        ("How do I upgrade to Premium?", "subscriptions"),
        ("How do I cancel my subscription?", "subscriptions"),
        # Subscription activation
        ("How do I subscribe to Premium?", "subscription_activation"),
        ("How do I activate my subscription?", "subscription_activation"),
        # Subscription renewal
        ("Does my plan renew automatically?", "subscription_renewal"),
        ("My subscription expired", "subscription_renewal"),
        ("What happens when my plan ends?", "subscription_renewal"),
        ("How do I renew my subscription?", "subscription_renewal"),
        ("How do I change my plan?", "subscriptions"),
        ("How much does the Pro plan cost?", "subscriptions"),
        # Billing
        ("Where can I download my invoice?", "billing"),
        ("I was charged twice, can I get a refund?", "billing"),
        ("Show me my payment history", "billing"),
        # Profile management
        ("How do I update my profile?", "profile"),
        ("Can I change my email address?", "profile"),
        ("How do I change my username?", "profile"),
        ("How do I delete my account?", "profile"),
        # Dashboard analytics
        ("Where can I see my analytics?", "dashboard"),
        ("How many views did my posts get?", "dashboard"),
        ("What do the dashboard charts show?", "dashboard"),
        # Notifications
        ("How do notifications work?", "notifications"),
        ("Do I get email notifications?", "notifications"),
        ("What does the bell icon do?", "notifications"),
        # General platform FAQs
        # Password / login
        ("I forgot my password", "password_login"),
        ("How do I log in?", "password_login"),
        ("I can't log in", "password_login"),
        ("My session expired", "password_login"),
        ("Why was I logged out? It says unauthorized", "password_login"),
        ("How do I reset my password?", "password_login"),
        # General platform usage
        ("What is this platform about?", "general"),
        ("How do I register?", "general"),
        ("How do I sign up?", "general"),
        ("How do I search for posts by other people?", "general"),
    ],
)
def test_question_matches_expected_topic(question, expected_topic):
    result = get_support_response(question)
    assert result.topic == expected_topic, f"{question!r} matched {result.topic!r}"
    assert result.response == _answer_for(expected_topic)
    assert result.matched is True


@pytest.mark.parametrize(
    "question",
    ["HOW DO I CREATE A POST?", "how do i create a post", "  How   do I create... a POST!!!  "],
)
def test_matching_ignores_case_whitespace_and_punctuation(question):
    assert get_support_response(question).topic == "create_post"


def test_plan_questions_always_get_the_subscribe_endpoint():
    # "How do I get the Pro plan?" scores higher on the plans overview than on
    # activation; either answer must still tell the user how to start a plan.
    for question in ("How do I get the Pro plan?", "How do I subscribe?", "What plans are there?"):
        assert "POST /subscriptions/subscribe" in get_support_response(question).response, question


def test_plural_and_singular_forms_both_match():
    assert get_support_response("Where are my invoices?").topic == "billing"
    assert get_support_response("Where is my invoice?").topic == "billing"


def test_edit_words_without_a_post_do_not_match_edit_post():
    # "change" alone is not about posts -- requires_any keeps these out of edit_post.
    assert get_support_response("How do I change my plan?").topic == "subscriptions"
    assert get_support_response("How do I change my email?").topic == "profile"


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def test_fallback_text():
    assert FALLBACK_RESPONSE == (
        "I'm not sure about that yet. Please try asking about posts, comments, subscriptions, billing, "
        "your profile, dashboard analytics, or notifications."
    )


@pytest.mark.parametrize(
    "question",
    [
        "Tell me a joke",
        "What is the weather today?",
        "What's the weather like on Mars?",
        "I would like a pizza",
        "",
        "   ",
        "?!.",
        "12345",
    ],
)
def test_unknown_questions_get_fallback(question):
    result = get_support_response(question)
    assert result == SupportResponse(topic=None, response=FALLBACK_RESPONSE)
    assert result.matched is False
    assert match_topic(question) is None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_same_question_always_gets_same_answer():
    question = "How do I cancel my subscription and get my invoice?"
    answers = {get_support_response(question) for _ in range(20)}
    assert len(answers) == 1


def test_ties_go_to_the_topic_listed_first():
    # "edit" (edit_post) and "delete" (delete_post) score one hit each;
    # edit_post is listed first, so it wins every time.
    order = [topic.slug for topic in FAQ_TOPICS]
    assert order.index("edit_post") < order.index("delete_post")
    assert get_support_response("edit or delete my post").topic == "edit_post"


def test_module_has_no_database_or_web_dependencies():
    # Kept importable and usable on its own (e.g. from scripts or other services).
    tree = ast.parse(open(support_faq.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"re", "dataclasses"}
