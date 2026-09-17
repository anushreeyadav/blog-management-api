"""add active subscription plan to user

Revision ID: 3f0f35fc05d5
Revises: 1347f08b4908
Create Date: 2026-09-17 14:46:01.805361

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f0f35fc05d5'
down_revision: Union[str, Sequence[str], None] = '1347f08b4908'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable at first so existing rows (Do not reset existing users) can
    # be backfilled before the NOT NULL constraint is enforced.
    op.add_column('users', sa.Column('subscription_plan_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_users_subscription_plan_id'), 'users', ['subscription_plan_id'], unique=False)
    op.create_foreign_key(
        op.f('users_subscription_plan_id_fkey'),
        'users', 'subscription_plans', ['subscription_plan_id'], ['id'], ondelete='RESTRICT',
    )

    # Every existing user gets the Basic plan by default.
    op.execute(
        "UPDATE users SET subscription_plan_id = "
        "(SELECT id FROM subscription_plans WHERE slug = 'basic') "
        "WHERE subscription_plan_id IS NULL"
    )

    op.alter_column('users', 'subscription_plan_id', nullable=False)


def downgrade() -> None:
    op.drop_constraint(op.f('users_subscription_plan_id_fkey'), 'users', type_='foreignkey')
    op.drop_index(op.f('ix_users_subscription_plan_id'), table_name='users')
    op.drop_column('users', 'subscription_plan_id')
