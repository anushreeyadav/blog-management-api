"""add auth0_sub to users

Revision ID: c81f4a2d9e70
Revises: b7d2e4f19a3c
Create Date: 2026-09-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c81f4a2d9e70'
down_revision: Union[str, Sequence[str], None] = 'b7d2e4f19a3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable: every existing (username/password) user keeps NULL here.
    # Skips whatever already exists, since app startup's
    # ensure_user_auth0_sub_column (app/database.py) may have added it first.
    inspector = sa.inspect(op.get_bind())
    if 'auth0_sub' not in {col['name'] for col in inspector.get_columns('users')}:
        op.add_column('users', sa.Column('auth0_sub', sa.String(length=255), nullable=True))
    if 'ix_users_auth0_sub' not in {idx['name'] for idx in inspector.get_indexes('users')}:
        op.create_index(op.f('ix_users_auth0_sub'), 'users', ['auth0_sub'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_users_auth0_sub'), table_name='users')
    op.drop_column('users', 'auth0_sub')
