"""one chronicle story per attendee; Discord usernames for picking co-hosts

Revision ID: 0003
Revises: 0002

- chronicle_entries: the one-entry-per-event rule becomes one entry per (event, author),
  so every attendee can tell their own story. Existing rows are unchanged (each event
  already has a single entry, written by the host).
- members.username: the unique Discord username, saved at sign-in, so co-hosts can be
  chosen by name instead of numeric ID. Empty until the member next signs in.
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint('chronicle_entries_event_id_key', 'chronicle_entries', type_='unique')
    op.create_index('ix_chronicle_entries_event_id', 'chronicle_entries', ['event_id'])
    op.create_unique_constraint('ux_chron_event_author', 'chronicle_entries', ['event_id', 'author_id'])
    op.add_column('members', sa.Column('username', sa.String(length=40), server_default=sa.text("''"),
                                       nullable=False))
    op.create_index('ix_members_username', 'members', [sa.literal_column('lower(username)')],
                    postgresql_where=sa.text("username <> ''"))


def downgrade() -> None:
    op.drop_index('ix_members_username', table_name='members')
    op.drop_column('members', 'username')
    op.drop_constraint('ux_chron_event_author', 'chronicle_entries', type_='unique')
    op.drop_index('ix_chronicle_entries_event_id', table_name='chronicle_entries')
    # fails if an event already has several stories: remove the extra ones first
    op.create_unique_constraint('chronicle_entries_event_id_key', 'chronicle_entries', ['event_id'])
