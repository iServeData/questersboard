"""event voice channels (FU-1)

Revision ID: 0005
Revises: 0004

- events.discord_voice_channel_id: the event's voice channel while it is open.
- events.discord_voice_members: the members the bot gave access (so people who leave can be removed).
- events.voice_empty_since: when the channel was last seen empty (written by the bot and worker).
- events.voice_closed_at: set when the channel was removed for being empty, so it isn't reopened
  automatically; a host can reopen it from the Organizer menu.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

COLUMNS = "discord_voice_channel_id, discord_voice_members, voice_empty_since, voice_closed_at"

# The worker opens, fills and removes voice channels, so it may update just these columns.
GRANTS = rf"""
DO $grants$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'questersboard_worker') THEN
    GRANT UPDATE ({COLUMNS}) ON events TO questersboard_worker;
  END IF;
END
$grants$;
"""
REVOKES = GRANTS.replace("GRANT UPDATE", "REVOKE UPDATE").replace(" TO questersboard_worker", " FROM questersboard_worker")


def upgrade() -> None:
    op.add_column('events', sa.Column('discord_voice_channel_id', sa.String(length=22), nullable=True))
    op.add_column('events', sa.Column('discord_voice_members', postgresql.JSONB(), server_default=sa.text("'[]'"),
                                      nullable=False))
    op.add_column('events', sa.Column('voice_empty_since', sa.DateTime(timezone=True), nullable=True))
    op.add_column('events', sa.Column('voice_closed_at', sa.DateTime(timezone=True), nullable=True))
    op.execute(GRANTS)


def downgrade() -> None:
    op.execute(REVOKES)
    for c in ('voice_closed_at', 'voice_empty_since', 'discord_voice_members', 'discord_voice_channel_id'):
        op.drop_column('events', c)
