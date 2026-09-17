from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html

from .models import BillingHistory, SubscriptionPlan


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "max_posts", "max_images", "max_likes", "max_comments", "is_active")
    search_fields = ("name", "slug")
    list_filter = ("is_active", "billing_interval")
    ordering = ("price",)


@admin.register(BillingHistory)
class BillingHistoryAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "plan",
        "transaction_id",
        "amount",
        "start_date",
        "end_date",
        "status",
        "invoice_link",
        "created_at",
    )
    search_fields = ("user__username", "user__email", "transaction_id")
    list_filter = ("status", "plan", "start_date", "end_date", "created_at")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def get_queryset(self, request):
        # user/plan are shown on every row -- avoid an extra query per row.
        return super().get_queryset(request).select_related("user", "plan")

    @admin.display(description="Invoice")
    def invoice_link(self, obj: BillingHistory):
        if not obj.invoice_path:
            return "-"
        # invoice_path is a same-origin-relative /media/invoices/<file> URL
        # on the FASTAPI app (see app/services/invoices.py), never a raw
        # filesystem path -- but this admin runs on its own origin/port, so
        # a bare relative link here would 404 against this site instead.
        # FASTAPI_BASE_URL (configurable, see settings.py) makes it an
        # absolute link to where that file is actually served.
        url = f"{settings.FASTAPI_BASE_URL}{obj.invoice_path}"
        return format_html('<a href="{}" target="_blank" rel="noopener noreferrer">View invoice</a>', url)
