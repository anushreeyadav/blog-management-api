"""add dashboard performance indexes

Revision ID: 2727132c9ddf
Revises: e603a7e84f3b
Create Date: 2026-09-22 12:52:50.236685

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2727132c9ddf'
down_revision: Union[str, Sequence[str], None] = 'e603a7e84f3b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Purely additive -- no column, data, or existing-index change. Each of
    # these columns is filtered directly by the dashboard's aggregation
    # queries (app/services/dashboard_service.py) and by pre-existing
    # endpoints (GET /posts/mine, GET /posts/{id}/comments); without an
    # index each becomes a full table scan as the table grows.
    op.create_index(op.f('ix_posts_author_id'), 'posts', ['author_id'], unique=False)
    op.create_index(op.f('ix_comments_post_id'), 'comments', ['post_id'], unique=False)
    op.create_index(op.f('ix_comments_user_id'), 'comments', ['user_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_comments_user_id'), table_name='comments')
    op.drop_index(op.f('ix_comments_post_id'), table_name='comments')
    op.drop_index(op.f('ix_posts_author_id'), table_name='posts')
