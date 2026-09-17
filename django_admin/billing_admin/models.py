"""
Read-mostly models over tables the FastAPI app's own SQLAlchemy models and
Alembic migrations already own (see ../../app/models.py and
../../alembic/versions/). Every model here is `managed = False`: Django
never creates, alters, or drops these tables -- `python manage.py migrate`
only touches django.contrib.*'s own tables (auth_user, django_session,
...), never these.

AppUser exists only so BillingHistory can show/search/filter by the
subscriber's username and email; it is intentionally not registered with
the admin site (Sub-Task 15 asks to register SubscriptionPlan and
BillingHistory, not User).
"""

from django.db import models


class AppUser(models.Model):
    username = models.CharField(max_length=50)
    email = models.CharField(max_length=255)
    is_admin = models.BooleanField()

    class Meta:
        managed = False
        db_table = "users"
        verbose_name = "App User"

    def __str__(self) -> str:
        return self.username


class SubscriptionPlan(models.Model):
    name = models.CharField(max_length=50)
    slug = models.CharField(max_length=50)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    billing_interval = models.CharField(max_length=10)
    max_posts = models.IntegerField(null=True, blank=True)
    max_images = models.IntegerField(null=True, blank=True)
    max_likes = models.IntegerField(null=True, blank=True)
    max_comments = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "subscription_plans"

    def __str__(self) -> str:
        return self.name


class BillingHistory(models.Model):
    user = models.ForeignKey(
        AppUser, on_delete=models.DO_NOTHING, db_column="user_id", related_name="billing_history"
    )
    plan = models.ForeignKey(
        SubscriptionPlan, on_delete=models.DO_NOTHING, db_column="subscription_plan_id", related_name="billing_history"
    )
    transaction_id = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    status = models.CharField(max_length=20)
    invoice_path = models.CharField(max_length=500, null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "billing_history"
        verbose_name_plural = "Billing history"

    def __str__(self) -> str:
        return self.transaction_id
