"""Solo adventure stories

Revision ID: 0010
Revises: 0009

- events.kind: "event" (every existing row) or "solo", the private record behind a Solo adventure
  story. Solo records are never listed, posted to Discord or put in calendars.
- public_chronicle.kind: the same, so the public Chronicle can show solo stories in their own section.
- chronicle_interest: "Join me next time" on a solo story (who would like to come along). The app reads
  and writes it; the worker only reads it, to tell those people when the author plans a group event.
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

GRANTS = r"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON chronicle_interest TO questersboard_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') THEN
    GRANT SELECT ON chronicle_interest TO questersboard_worker;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.add_column('events', sa.Column('kind', sa.String(8), nullable=False, server_default='event'))
    op.create_check_constraint('ck_event_kind', 'events', "kind in ('event','solo')")
    op.add_column('public_chronicle', sa.Column('kind', sa.String(8), nullable=False, server_default='event'))
    op.create_table(
        'chronicle_interest',
        sa.Column('entry_id', sa.BigInteger(), sa.ForeignKey('chronicle_entries.id', ondelete='CASCADE'),
                  primary_key=True),
        sa.Column('member_id', sa.BigInteger(), sa.ForeignKey('members.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('ix_chronicle_interest_member_id', 'chronicle_interest', ['member_id'])
    op.execute(GRANTS)


def downgrade() -> None:
    op.drop_table('chronicle_interest')
    op.drop_column('public_chronicle', 'kind')
    op.drop_constraint('ck_event_kind', 'events', type_='check')
    op.drop_column('events', 'kind')
