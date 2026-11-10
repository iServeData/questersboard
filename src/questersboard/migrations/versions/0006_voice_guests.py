"""event voice: guest links, session bans, leavers keep their seat

Revision ID: 0006
Revises: 0005

- event_voice_invites: one guest link per inviter per running event.
- event_voice_guests: who joined voice with a guest link (Discord ID only), while the event runs.
- event_voice_bans: banned from one event's voice for the rest of that event.
All three are deleted when the event ends or is cancelled.
- events.discord_voice_members becomes {discord_id: "in" | "left" | "banned"} (it was a list; voice was
  off everywhere, so it is reset to {}).
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

TABLES = "event_voice_invites, event_voice_guests, event_voice_bans"

GRANTS = rf"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLES} TO questersboard_app;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') THEN
    GRANT SELECT, DELETE ON {TABLES} TO questersboard_worker;   -- reads for syncing; deletes when events end
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_readonly') THEN
    GRANT SELECT ON {TABLES} TO questersboard_readonly;
  END IF;
END
$grants$;
"""


def upgrade() -> None:
    op.create_table('event_voice_invites',
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('event_id', sa.BigInteger(), nullable=False),
        sa.Column('created_by', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by'], ['members.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('code'),
        sa.UniqueConstraint('event_id', 'created_by', name='uq_event_voice_invites_inviter'))
    op.create_table('event_voice_guests',
        sa.Column('event_id', sa.BigInteger(), nullable=False),
        sa.Column('discord_id', sa.String(length=22), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('joined_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('event_id', 'discord_id'))
    op.create_table('event_voice_bans',
        sa.Column('event_id', sa.BigInteger(), nullable=False),
        sa.Column('discord_id', sa.String(length=22), nullable=False),
        sa.Column('banned_by', sa.BigInteger(), nullable=True),
        sa.Column('banned_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['event_id'], ['events.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['banned_by'], ['members.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('event_id', 'discord_id'))
    op.execute("UPDATE events SET discord_voice_members = '{}'::jsonb")
    op.alter_column('events', 'discord_voice_members', server_default=sa.text("'{}'::jsonb"))
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute("UPDATE events SET discord_voice_members = '[]'::jsonb")
    op.alter_column('events', 'discord_voice_members', server_default=sa.text("'[]'"))
    op.drop_table('event_voice_bans')
    op.drop_table('event_voice_guests')
    op.drop_table('event_voice_invites')
