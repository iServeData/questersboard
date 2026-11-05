"""SQLAlchemy models. Schema changes go through Alembic (src/wowquester/migrations)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
                        LargeBinary, SmallInteger, String, Text, UniqueConstraint, func, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- identity

class Member(Base):
    __tablename__ = "members"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    discord_id: Mapped[str] = mapped_column(String(22), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    username: Mapped[str] = mapped_column(String(40), nullable=False, default="", server_default="")
    recognition_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_members_username", text("lower(username)"), postgresql_where=text("username <> ''")),)


class Session(Base):
    __tablename__ = "web_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"))
    csrf: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalendarToken(Base):
    __tablename__ = "calendar_tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = _now()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BattlenetLink(Base):
    __tablename__ = "battlenet_links"
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), primary_key=True)
    region: Mapped[str] = mapped_column(String(4), primary_key=True)
    snapshot_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # Fernet(field-key)
    linked_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- roster

class Character(Base):
    __tablename__ = "characters"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False, index=True)
    edition: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(4), nullable=False)
    ruleset: Mapped[str | None] = mapped_column(String(32))
    realm_slug: Mapped[str | None] = mapped_column(String(64))
    realm_name: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    faction: Mapped[str] = mapped_column(String(16), nullable=False)
    race_key: Mapped[str] = mapped_column(String(48), nullable=False)
    class_key: Mapped[str] = mapped_column(String(48), nullable=False)
    level: Mapped[int | None] = mapped_column(Integer)
    spec: Mapped[str | None] = mapped_column(String(48))
    eligible_roles: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False, default=list)
    preferred_role: Mapped[str | None] = mapped_column(String(32))
    focus_tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False, default=list)
    notes: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    recognition_default: Mapped[bool | None] = mapped_column(Boolean)
    verification: Mapped[str] = mapped_column(String(24), nullable=False, default="self_reported")
    api_external_id: Mapped[str | None] = mapped_column(String(64))
    api_namespace: Mapped[str | None] = mapped_column(String(64))
    api_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    member: Mapped[Member] = relationship()
    __table_args__ = (
        CheckConstraint("verification in ('self_reported','api_matched','ownership_verified')", name="ck_char_verif"),
        CheckConstraint("level is null or (level between 1 and 100)", name="ck_char_level"),
        CheckConstraint("(realm_slug is null) or (ruleset is null)", name="ck_char_realm_xor_ruleset"),
        Index("ux_char_owner_name", "member_id", "edition", "region", text("coalesce(realm_slug,'')"),
              text("coalesce(ruleset,'')"), text("lower(name)"), unique=True,
              postgresql_where=text("archived_at is null")),
        Index("ux_char_verified", "edition", "region", "api_external_id", unique=True,
              postgresql_where=text("verification = 'ownership_verified' and archived_at is null")),
    )


class ReferenceOption(Base):
    """Races, classes, race/class combos, specs, zones, quests, locations. Provenance recorded per row."""
    __tablename__ = "reference_options"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    edition: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(96), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    parent_key: Mapped[str | None] = mapped_column(String(96))
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(16), nullable=False)        # seed | blizzard | manual
    source_ref: Mapped[str | None] = mapped_column(String(300))           # URL or namespace
    external_id: Mapped[str | None] = mapped_column(String(64))
    verification: Mapped[str] = mapped_column(String(16), nullable=False, default="unverified")
    refreshed_at: Mapped[datetime] = _now()
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("edition", "kind", "key", "parent_key", name="ux_ref_key"),
        CheckConstraint("kind in ('race','class','race_class','spec','zone','quest','location','realm')",
                        name="ck_ref_kind"),
        CheckConstraint("source in ('seed','blizzard','manual')", name="ck_ref_source"),
    )


# --------------------------------------------------------------------------- events

EVENT_STATES = ("draft", "scheduled", "in_progress", "completed", "cancelled")
SIGNUP_STATES = ("pending", "confirmed", "tentative", "waitlisted", "withdrawn", "rejected")


class Series(Base):
    __tablename__ = "event_series"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    rrule: Mapped[str] = mapped_column(String(300), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    local_start: Mapped[str] = mapped_column(String(19), nullable=False)  # first occurrence, local ISO
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _now()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    edition: Mapped[str] = mapped_column(String(32), nullable=False)
    region: Mapped[str] = mapped_column(String(4), nullable=False)
    ruleset: Mapped[str | None] = mapped_column(String(32))
    realm_slug: Mapped[str | None] = mapped_column(String(64))
    faction: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    eligibility: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    objective: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    difficulty: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    level_min: Mapped[int | None] = mapped_column(SmallInteger)
    level_max: Mapped[int | None] = mapped_column(SmallInteger)
    cover_image: Mapped[str | None] = mapped_column(String(80))
    host_character_id: Mapped[int | None] = mapped_column(ForeignKey("characters.id", ondelete="SET NULL"))
    discord_thread_id: Mapped[str | None] = mapped_column(String(22))
    discord_thread_members: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Per-event voice channel ([voice] enabled): opened on Start, deleted on End/Cancel or when it has been
    # empty past the scheduled end. voice_members are the members given access, so leavers can be removed.
    discord_voice_channel_id: Mapped[str | None] = mapped_column(String(22))
    discord_voice_members: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    voice_empty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voice_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creator_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    signup_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    signup_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    participant_limit: Mapped[int | None] = mapped_column(Integer)
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    late_join: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    chronicle_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    public_listing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    activity_location: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    muster_location: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("event_series.id"))
    series_index: Mapped[int | None] = mapped_column(Integer)
    series_detached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    copied_from_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    discord_channel_id: Mapped[str | None] = mapped_column(String(22))
    discord_message_id: Mapped[str | None] = mapped_column(String(22))
    cancelled_reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    creator: Mapped[Member] = relationship(foreign_keys=[creator_id])
    roles: Mapped[list["EventRole"]] = relationship(order_by="EventRole.position", cascade="all, delete-orphan")
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="ck_event_interval"),
        CheckConstraint("state in ('draft','scheduled','in_progress','completed','cancelled')", name="ck_event_state"),
        CheckConstraint("signup_policy in ('open','approval')", name="ck_event_policy"),
        CheckConstraint("participant_limit is null or participant_limit > 0", name="ck_event_limit"),
        CheckConstraint("level_min is null or level_max is null or level_min <= level_max", name="ck_event_levels"),
        Index("ix_events_state_start", "state", "start_at"),
    )


class EventCohost(Base):
    __tablename__ = "event_cohosts"
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)


class EventRole(Base):
    __tablename__ = "event_roles"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    key: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("event_id", "key", name="ux_event_role"),
                      CheckConstraint("quantity between 1 and 80", name="ck_role_qty"))


class Signup(Base):
    __tablename__ = "signups"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    role_key: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    recognition: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    needs_reconfirm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    character: Mapped[Character] = relationship(foreign_keys=[character_id])
    member: Mapped[Member] = relationship(foreign_keys=[member_id])
    __table_args__ = (
        CheckConstraint("state in ('pending','confirmed','tentative','waitlisted','withdrawn','rejected')",
                        name="ck_signup_state"),
        # one active signup per person per event: no second character, no second slot
        Index("ux_signup_active_member", "event_id", "member_id", unique=True,
              postgresql_where=text("state in ('pending','confirmed','tentative','waitlisted')")),
    )


class Attendance(Base):
    __tablename__ = "attendance"
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)
    character_id: Mapped[int | None] = mapped_column(ForeignKey("characters.id"))
    role_key: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="unknown")
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    recorded_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("status in ('attended','absent','unknown')", name="ck_att_status"),)


class SchedulePolicy(Base):
    __tablename__ = "schedule_policy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_simultaneous: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("id = 1", name="ck_policy_single"),
                      CheckConstraint("max_simultaneous >= 1", name="ck_policy_positive"))


class EventReservation(Base):
    """Community-wide WoW capacity. Only WoWQuester events live here; other Discord events never do."""
    __tablename__ = "event_reservations"
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    during = mapped_column(TSTZRANGE, nullable=False)
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class MemberReservation(Base):
    """One row per person per event (roles merged). Exclusion constraint forbids overlaps per member."""
    __tablename__ = "member_reservations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    during = mapped_column(TSTZRANGE, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    __table_args__ = (UniqueConstraint("member_id", "event_id", name="ux_member_event"),
                      CheckConstraint("kind in ('organizer','cohost','participant')", name="ck_mres_kind"))


class ReminderDelivery(Base):
    __tablename__ = "reminder_deliveries"
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)
    offset_minutes: Mapped[int] = mapped_column(Integer, primary_key=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    sent_at: Mapped[datetime] = _now()


# --------------------------------------------------------------------------- chronicle

CHRONICLE_STATES = ("draft", "edited", "pending_review", "changes_requested", "rejected", "published", "unpublished")


class ChronicleEntry(Base):
    __tablename__ = "chronicle_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    author_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    working_revision_id: Mapped[int | None] = mapped_column(BigInteger)
    published_revision_id: Mapped[int | None] = mapped_column(BigInteger)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unpublished_reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    banner_upload_id: Mapped[int | None] = mapped_column(ForeignKey("chronicle_uploads.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint(
        "state in ('draft','edited','pending_review','changes_requested','rejected','published','unpublished')",
        name="ck_chron_state"),
        UniqueConstraint("event_id", "author_id", name="ux_chron_event_author"))


class ChronicleCoauthor(Base):
    __tablename__ = "chronicle_coauthors"
    entry_id: Mapped[int] = mapped_column(ForeignKey("chronicle_entries.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), primary_key=True)


class ChronicleRevision(Base):
    __tablename__ = "chronicle_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("chronicle_entries.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(140), nullable=False)
    attempted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    happened: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    moments: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ai_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = _now()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    decision: Mapped[str | None] = mapped_column(String(20))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    __table_args__ = (UniqueConstraint("entry_id", "number", name="ux_rev_number"),
                      CheckConstraint("outcome in ('completed','partial','not_completed','social','unknown')",
                                      name="ck_rev_outcome"))


class ChronicleUpload(Base):
    __tablename__ = "chronicle_uploads"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    entry_id: Mapped[int] = mapped_column(ForeignKey("chronicle_entries.id", ondelete="CASCADE"), nullable=False)
    filename: Mapped[str] = mapped_column(String(80), nullable=False)
    mime: Mapped[str] = mapped_column(String(24), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    caption: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    cleared_by: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class RecognitionRemoval(Base):
    __tablename__ = "recognition_removals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    requested_at: Mapped[datetime] = _now()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- public projections
# The ONLY tables the public web routes and the readonly role read.

class PublicChronicle(Base):
    __tablename__ = "public_chronicle"
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(140), nullable=False)
    summary: Mapped[str] = mapped_column(String(400), nullable=False)
    body_html: Mapped[str] = mapped_column(Text, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    edition_name: Mapped[str] = mapped_column(String(64), nullable=False)
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    participants: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    images: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    banner: Mapped[dict | None] = mapped_column(JSONB)
    details: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    ai_assisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicEvent(Base):
    __tablename__ = "public_events"
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    edition_name: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset: Mapped[str | None] = mapped_column(String(32))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    location_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    muster_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    roles: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # counts only, no names
    signup_policy: Mapped[str] = mapped_column(String(16), nullable=False)
    exclusive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PublicEventVoice(Base):
    """Head count in an event's voice channel (written by the bot; counts only, no IDs or names).

    Empty until per-event voice channels exist (docs/followups.md FU-1). The status card shows a
    count only while observed_at is fresh."""
    __tablename__ = "public_event_voice"
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public_events.public_id", ondelete="CASCADE"), primary_key=True)
    in_voice: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("in_voice >= 0", name="ck_public_event_voice_nonneg"),)


class ServiceHeartbeat(Base):
    """Last check-in from the bot and worker, for the public status card. No secrets, no IDs."""
    __tablename__ = "service_heartbeats"
    name: Mapped[str] = mapped_column(String(16), primary_key=True)          # bot | worker
    state: Mapped[str] = mapped_column(String(16), nullable=False)          # bot: connected | disconnected
    beat_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime] = _now()
    __table_args__ = (CheckConstraint("name in ('bot', 'worker')", name="ck_service_heartbeats_name"),)


# --------------------------------------------------------------------------- infrastructure

class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    run_after: Mapped[datetime] = _now()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()
    __table_args__ = (
        # pending jobs with the same kind+key coalesce (e.g. many edits -> one Discord post update)
        Index("ux_outbox_pending", "kind", "key", unique=True,
              postgresql_where=text("done_at is null and dead_at is null")),
        Index("ix_outbox_due", "run_after", postgresql_where=text("done_at is null and dead_at is null")),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _now()
    actor_discord_id: Mapped[str | None] = mapped_column(String(22))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (Index("ix_audit_target", "target_type", "target_id"),)


class ConfigChange(Base):
    __tablename__ = "config_changes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = _now()
    service: Mapped[str] = mapped_column(String(16), nullable=False)
    sections: Mapped[list] = mapped_column(JSONB, nullable=False)
    hashes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class ApiCache(Base):
    __tablename__ = "api_cache"
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    fetched_at: Mapped[datetime] = _now()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiProbe(Base):
    __tablename__ = "api_probes"
    edition: Mapped[str] = mapped_column(String(32), primary_key=True)
    region: Mapped[str] = mapped_column(String(4), primary_key=True)
    capability: Mapped[str] = mapped_column(String(16), primary_key=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    probed_at: Mapped[datetime] = _now()


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def _server_defaults() -> None:
    """Mirror simple Python defaults as server defaults so raw SQL (outbox, worker) gets them too."""
    import json as _json

    from sqlalchemy.schema import DefaultClause

    def _sd(col, sql):
        DefaultClause(text(sql))._set_parent(col)

    from sqlalchemy import ARRAY as _ARRAY
    from sqlalchemy.dialects.postgresql import JSONB as _JSONB
    for table in Base.metadata.tables.values():
        for col in table.columns:
            d = col.default
            if col.server_default is not None or d is None or not getattr(d, "is_scalar", False) and \
                    not getattr(d, "is_callable", False):
                continue
            if getattr(d, "is_callable", False):
                fn = getattr(d.arg, "__wrapped__", d.arg)
                name = getattr(fn, "__name__", "")
                if isinstance(col.type, _JSONB) and name in ("dict", "list"):
                    _sd(col, "'{}'::jsonb" if name == "dict" else "'[]'::jsonb")
                elif isinstance(col.type, _ARRAY) and name == "list":
                    _sd(col, "'{}'")
                elif name == "uuid4":
                    _sd(col, "gen_random_uuid()")
                continue
            v = d.arg
            if isinstance(v, bool):
                _sd(col, "true" if v else "false")
            elif isinstance(v, (int, float)):
                _sd(col, str(v))
            elif isinstance(v, str):
                _sd(col, "'" + v.replace("'", "''") + "'")
            elif isinstance(v, (dict, list)):
                _sd(col, "'" + _json.dumps(v) + "'::jsonb")


_server_defaults()
