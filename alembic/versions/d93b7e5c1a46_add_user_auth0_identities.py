"""add user_auth0_identities

Revision ID: d93b7e5c1a46
Revises: c81f4a2d9e70
Create Date: 2026-09-25 16:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd93b7e5c1a46'
down_revision: Union[str, Sequence[str], None] = 'c81f4a2d9e70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Skips whatever already exists: app startup's Base.metadata.create_all
    # (app/main.py) may have created the table before this migration ran.
    inspector = sa.inspect(op.get_bind())
    if 'user_auth0_identities' not in inspector.get_table_names():
        op.create_table('user_auth0_identities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('auth0_sub', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
        )
    existing_indexes = {idx['name'] for idx in sa.inspect(op.get_bind()).get_indexes('user_auth0_identities')}
    if 'ix_user_auth0_identities_id' not in existing_indexes:
        op.create_index(op.f('ix_user_auth0_identities_id'), 'user_auth0_identities', ['id'], unique=False)
    if 'ix_user_auth0_identities_user_id' not in existing_indexes:
        op.create_index(op.f('ix_user_auth0_identities_user_id'), 'user_auth0_identities', ['user_id'], unique=False)
    if 'ix_user_auth0_identities_auth0_sub' not in existing_indexes:
        op.create_index(op.f('ix_user_auth0_identities_auth0_sub'), 'user_auth0_identities', ['auth0_sub'], unique=True)

    # Backfill: every user already linked via users.auth0_sub gets the same
    # identity here. users.auth0_sub itself is left untouched.
    op.execute(
        """
        INSERT INTO user_auth0_identities (user_id, auth0_sub)
        SELECT u.id, u.auth0_sub FROM users u
        WHERE u.auth0_sub IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM user_auth0_identities i WHERE i.auth0_sub = u.auth0_sub)
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_user_auth0_identities_auth0_sub'), table_name='user_auth0_identities')
    op.drop_index(op.f('ix_user_auth0_identities_user_id'), table_name='user_auth0_identities')
    op.drop_index(op.f('ix_user_auth0_identities_id'), table_name='user_auth0_identities')
    op.drop_table('user_auth0_identities')
