"""Tentative -> in: the "I'm in" button, and Join again from Discord while tentative."""
from __future__ import annotations

import pytest
from conftest import actor, needs_db
from helpers import char, event
from wowquester.domain import signups
from wowquester.domain.errors import Conflict

pytestmark = needs_db

HOST = actor(1, "event_creator")


def test_confirm_moves_tentative_in(seeded, cfg):
    p = actor(60)
    with seeded.tx() as s:
        ev = event(s, cfg, HOST, 0, 2)
        c = char(s, cfg, p, "Zulmara")
        assert signups.join(s, cfg, p, ev.id, c.id, tentative=True).state == "tentative"
        assert signups.confirm(s, cfg, p, ev.id).state == "confirmed"
        with pytest.raises(Conflict):
            signups.confirm(s, cfg, p, ev.id)               # already in


def test_join_again_while_tentative_upgrades(seeded, cfg):
    p = actor(61)
    with seeded.tx() as s:
        ev = event(s, cfg, HOST, 0, 2)
        c = char(s, cfg, p, "Vashka")
        signups.join(s, cfg, p, ev.id, c.id, tentative=True)
        su = signups.join(s, cfg, p, ev.id, c.id)           # Discord Join button again
        assert su.state == "confirmed"
        with pytest.raises(Conflict):
            signups.join(s, cfg, p, ev.id, c.id)            # a real duplicate still refuses


def test_confirm_on_approval_event_asks_the_host(seeded, cfg):
    p = actor(62)
    with seeded.tx() as s:
        ev = event(s, cfg, HOST, 0, 2, signup_policy="approval")
        c = char(s, cfg, p, "Corwin")
        signups.join(s, cfg, p, ev.id, c.id, tentative=True)
        assert signups.confirm(s, cfg, p, ev.id).state == "pending"


def test_confirm_when_full_goes_to_waitlist(seeded, cfg):
    p, q = actor(63), actor(64)
    with seeded.tx() as s:
        ev = event(s, cfg, HOST, 0, 2, participant_limit=1)
        signups.join(s, cfg, q, ev.id, char(s, cfg, q, "Elspeth").id)
        signups.join(s, cfg, p, ev.id, char(s, cfg, p, "Brightmere").id, tentative=True)
        assert signups.confirm(s, cfg, p, ev.id).state == "waitlisted"
