"""add post publishing status (status, scheduled_at, published_at)

Revision ID: f4a91c7e2b58
Revises: d93b7e5c1a46
Create Date: 2026-09-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a91c7e2b58'
down_revision: Union[str, Sequence[str], None] = 'd93b7e5c1a46'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Skips whatever already exists: app startup's
    # ensure_post_publishing_columns (app/database.py) may have added the
    # columns first.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col['name'] for col in inspector.get_columns('posts')}

    # Every existing post was already public, so it becomes "published".
    if 'status' not in columns:
        op.add_column(
            'posts',
            sa.Column('status', sa.String(length=20), server_default='published', nullable=False),
        )
    if 'scheduled_at' not in columns:
        op.add_column('posts', sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=True))
    if 'published_at' not in columns:
        op.add_column('posts', sa.Column('published_at', sa.DateTime(timezone=True), nullable=True))

    # Backfill: a published post went live when it was created.
    op.execute(
        "UPDATE posts SET published_at = created_at "
        "WHERE status = 'published' AND published_at IS NULL"
    )

    # SQLite can't add CHECK constraints to an existing table; there they
    # only exist on databases created fresh by Base.metadata.create_all.
    if bind.dialect.name != 'sqlite':
        existing_checks = {ck['name'] for ck in inspector.get_check_constraints('posts')}
        if 'ck_posts_status_valid' not in existing_checks:
            op.create_check_constraint(
                'ck_posts_status_valid', 'posts', "status IN ('draft', 'scheduled', 'published')"
            )
        if 'ck_posts_scheduled_requires_scheduled_at' not in existing_checks:
            op.create_check_constraint(
                'ck_posts_scheduled_requires_scheduled_at',
                'posts',
                "status <> 'scheduled' OR scheduled_at IS NOT NULL",
            )

    existing_indexes = {idx['name'] for idx in inspector.get_indexes('posts')}
    if 'ix_posts_status_scheduled_at' not in existing_indexes:
        op.create_index('ix_posts_status_scheduled_at', 'posts', ['status', 'scheduled_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_posts_status_scheduled_at', table_name='posts')
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('ck_posts_scheduled_requires_scheduled_at', 'posts', type_='check')
        op.drop_constraint('ck_posts_status_valid', 'posts', type_='check')
    op.drop_column('posts', 'published_at')
    op.drop_column('posts', 'scheduled_at')
    op.drop_column('posts', 'status')
