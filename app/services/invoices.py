"""
Invoice PDF generation, using ReportLab.

Follows the exact storage/URL convention app/services/media.py already
established for post images: files live under a subdirectory of the
project's existing MEDIA_ROOT (media/invoices/, alongside media/posts/) and
are referenced from the database only by their relative /media/invoices/
<file> URL -- never a local filesystem path, and never through a second,
separate static mount.
"""

from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

from app import models
from app.services.media import MEDIA_ROOT

INVOICES_DIR = MEDIA_ROOT / "invoices"
INVOICES_DIR.mkdir(parents=True, exist_ok=True)


def _invoice_number(transaction_id: str) -> str:
    """A human-facing document reference, distinct from the transaction id
    (the fake payment system's own reference) -- derived from the same
    unique suffix so it can't drift out of sync or collide independently."""
    return "INV-" + transaction_id.removeprefix("TXN-")


def generate_invoice_pdf(
    *,
    transaction_id: str,
    user: models.User,
    plan: models.SubscriptionPlan,
    amount: float,
    status: str,
    start_date: datetime,
    end_date: datetime,
) -> str:
    """Renders a one-page invoice PDF and returns its public /media/invoices/ URL."""
    filename = f"invoice_{transaction_id}.pdf"
    # INVOICES_DIR is read fresh here (not captured at def time) so tests
    # can point it at an isolated tmp directory via monkeypatch.
    destination = INVOICES_DIR / filename

    pdf = canvas.Canvas(str(destination), pagesize=A4)
    page_width, _ = A4
    left_margin = 2 * cm
    y = 27 * cm

    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(left_margin, y, "Invoice")
    y -= 1.2 * cm

    pdf.setFont("Helvetica", 10)
    pdf.line(left_margin, y, page_width - left_margin, y)
    y -= 0.9 * cm

    rows = [
        ("Invoice Number", _invoice_number(transaction_id)),
        ("Transaction ID", transaction_id),
        ("Issued", datetime.now().strftime("%Y-%m-%d")),
        ("Customer Name", user.username),
        ("Customer Email", user.email or "N/A"),
        ("Plan", plan.name),
        ("Plan Price", f"${plan.price:.2f}"),
        ("Amount Charged", f"${amount:.2f}"),
        ("Billing Status", status.capitalize()),
        ("Subscription Start", f"{start_date:%Y-%m-%d}"),
        ("Subscription End", f"{end_date:%Y-%m-%d}"),
    ]
    label_width = 4.5 * cm
    for label, value in rows:
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(left_margin, y, f"{label}:")
        pdf.setFont("Helvetica", 10)
        pdf.drawString(left_margin + label_width, y, str(value))
        y -= 0.75 * cm

    pdf.showPage()
    pdf.save()

    return f"/media/invoices/{filename}"
