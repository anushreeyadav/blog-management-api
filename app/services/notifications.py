import logging
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MAIL_HOST = os.getenv("MAIL_HOST", "smtp.example.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM") or MAIL_USERNAME
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").strip().lower() in ("1", "true", "yes")

# Development/test email mode: when not explicitly enabled, no real SMTP
# connection is ever attempted -- the notification is logged instead, so
# local runs and automated tests never need a real mail server or
# credentials. Set EMAIL_ENABLED=true in .env to send real email.
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def send_email(to_email: str, subject: str, body: str) -> None:
    """
    Sends a plain-text email, or -- when EMAIL_ENABLED is not "true" (the
    default) -- just logs what would have been sent.

    This function never raises. Any SMTP failure is caught and logged here;
    email delivery is best-effort and must never fail or roll back a
    request whose database work has already committed successfully, and
    must never leak SMTP host/credential details to the caller.
    """
    if not EMAIL_ENABLED:
        logger.info("[email disabled] to=%s subject=%r body=%r", to_email, subject, body)
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = MAIL_FROM
    message["To"] = to_email
    message.set_content(body)

    try:
        with smtplib.SMTP(MAIL_HOST, MAIL_PORT, timeout=10) as smtp:
            if MAIL_USE_TLS:
                smtp.starttls()
            if MAIL_USERNAME and MAIL_PASSWORD:
                smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
            smtp.send_message(message)
        logger.info("Email sent to=%s subject=%r", to_email, subject)
    except Exception:
        logger.exception("Failed to send email to=%s subject=%r", to_email, subject)


def _activity_notification_body(post_title: str, actor_username: str, activity: str) -> str:
    """The one fixed format every like/comment notification email uses:

        Post: "<title>"
        User: <actor_username>
        Activity: <activity>
        Time: <YYYY-MM-DD HH:MM AM/PM>

    Deliberately just these four fields, nothing else (e.g. no comment
    text) -- kept simple, clear, and professional per spec. The
    timestamp is captured here, at send time, in UTC -- the same
    timezone convention already used for every other timestamp in this
    app (Subscription/BillingHistory dates; see
    app/routers/subscriptions.py)."""
    when = datetime.now(timezone.utc)
    return (
        f'Post: "{post_title}"\n'
        f"User: {actor_username}\n"
        f"Activity: {activity}\n"
        f'Time: {when.strftime("%Y-%m-%d %I:%M %p")}'
    )


def send_comment_notification(post_owner_email: str, post_title: str, actor_username: str) -> None:
    subject = "New comment on your blog post"
    body = _activity_notification_body(post_title, actor_username, "Commented on your post")
    send_email(post_owner_email, subject, body)


def send_like_notification(post_owner_email: str, post_title: str, actor_username: str) -> None:
    subject = "Someone liked your blog post"
    body = _activity_notification_body(post_title, actor_username, "Liked your post")
    send_email(post_owner_email, subject, body)
