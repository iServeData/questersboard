"""status card: service heartbeats and per-event voice head counts

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

GRANTS = r"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON service_heartbeats, public_event_voice TO wowquester_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_worker') THEN
    GRANT SELECT ON service_heartbeats, public_event_voice TO wowquester_worker;
    GRANT INSERT, UPDATE (state, beat_at, started_at) ON service_heartbeats TO wowquester_worker;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wowquester_readonly') THEN
    GRANT SELECT ON service_heartbeats, public_event_voice TO wowquester_readonly;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.create_table('service_heartbeats',
    sa.Column('name', sa.String(length=16), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('beat_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("name in ('bot', 'worker')", name='ck_service_heartbeats_name'),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('public_event_voice',
    sa.Column('public_id', sa.UUID(), nullable=False),
    sa.Column('in_voice', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('in_voice >= 0', name='ck_public_event_voice_nonneg'),
    sa.ForeignKeyConstraint(['public_id'], ['public_events.public_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('public_id')
    )
    op.execute(GRANTS)


def downgrade() -> None:
    op.drop_table('public_event_voice')
    op.drop_table('service_heartbeats')
