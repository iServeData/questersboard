"""drafts sent for publishing

Revision ID: 0007
Revises: 0006

- events.publish_requested_at: set when a member without the event-creator role sends a draft for
  publishing. Moderators see these drafts in a "Waiting to be published" list; cleared on publish.
  The app role's table-level grants on events cover the new column; the worker only reads it.
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('events', sa.Column('publish_requested_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_events_publish_requested', 'events', ['publish_requested_at'],
                    postgresql_where=sa.text("state = 'draft' AND publish_requested_at IS NOT NULL"))


def downgrade() -> None:
    op.drop_index('ix_events_publish_requested', table_name='events')
    op.drop_column('events', 'publish_requested_at')
