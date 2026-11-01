"""Edition-aware Blizzard Battle.net API adapter.

Ground rules:
* Every call names an edition; its namespaces come from config. If an edition has no
  namespace for a capability, or config marks the capability "unsupported", the call
  raises Unsupported with a clear message. It NEVER falls back to Retail, Classic Era,
  Anniversary or PTR namespaces. PTR namespaces are rejected at config load.
* A character lookup confirms that Blizzard returns a character with that name on that
  realm. It does NOT prove the Discord member owns it (status "api_matched").
  Ownership is only "ownership_verified" after an optional Battle.net login (wow.profile)
  lists the character on the member's own account. The user token is used once and
  discarded; only an encrypted snapshot of character keys is stored.
* Client-credentials token cached in memory; responses cached in api_cache; retries with
  exponential backoff and Retry-After on 429/5xx; a per-process rate limiter.
"""

from __future__ import annotations

import json
import logging
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..config import Config, Edition
from ..models import ApiCache, ApiProbe, BattlenetLink, Character, ReferenceOption
from ..secrets import get_secret
from ..domain.timeutil import now

log = logging.getLogger("wowquester.blizzard")


class Unsupported(Exception):
    """The edition/capability isn't available from Blizzard (or not configured). Not an outage."""


class ApiError(Exception):
    pass


@dataclass
class CharacterMatch:
    found: bool
    detail: str
    level: int | None = None
    race: str | None = None
    klass: str | None = None
    faction: str | None = None
    external_id: str | None = None
    namespace: str | None = None


class _Limiter:
    def __init__(self, per_second: int):
        self.interval = 1.0 / max(1, per_second)
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            t = time.monotonic()
            if t < self.next:
                time.sleep(self.next - t)
            self.next = max(t, self.next) + self.interval


def slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("'", "")


class Blizzard:
    def __init__(self, cfg: Config, http: httpx.Client | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.bz = cfg["blizzard"]
        self.http = http or httpx.Client(timeout=self.bz.get("timeout_seconds", 10))
        self.sleep = sleep
        self._token: tuple[str, float] | None = None
        self.limiter = _Limiter(self.bz.get("requests_per_second", 20))

    # ------------------------------------------------------------------ plumbing
    def require(self, ed: Edition, cap: str, ns_kind: str, region: str) -> str:
        if not self.bz.get("enabled"):
            raise Unsupported("Blizzard API lookups are turned off in this installation.")
        status = ed.capability(cap)
        ns = ed.namespace(ns_kind, region)
        if status == "unsupported" or not ns:
            raise Unsupported(f"Blizzard doesn't provide {cap} data for {ed.name} ({region.upper()}) yet. "
                              "Entries stay self-reported.")
        return ns

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        cid = get_secret("blizzard-client-id")
        sec = get_secret("blizzard-client-secret")
        r = self._send("POST", f"{self.bz['oauth_base'].rstrip('/')}/token", auth=(cid.reveal(), sec.reveal()),
                       data={"grant_type": "client_credentials"})
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def _send(self, method, url, **kw) -> httpx.Response:
        tries = self.bz.get("max_retries", 3) + 1
        delay = 0.5
        last = None
        for i in range(tries):
            self.limiter.wait()
            try:
                r = self.http.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = f"network: {type(exc).__name__}"
            else:
                if r.status_code < 400 or r.status_code in (403, 404):
                    return r
                last = f"http {r.status_code}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    return r
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = max(delay, min(float(ra), 30.0))
            if i + 1 < tries:
                self.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise ApiError(f"Blizzard API unavailable ({last}).")

    def get(self, s: Session | None, region: str, path: str, namespace: str, cache_hours: float,
            locale: str = "en_US") -> tuple[int, dict]:
        key = f"{region}|{namespace}|{path}|{locale}"[:300]
        if s is not None:
            hit = s.get(ApiCache, key)
            if hit and hit.expires_at > now():
                return hit.status, hit.body
        url = self.cfg["blizzard"]["api_host_template"].replace("{region}", region) + path
        r = self._send("GET", url, params={"namespace": namespace, "locale": locale},
                       headers={"Authorization": f"Bearer {self.token()}"})
        status = r.status_code
        body = r.json() if status == 200 else {}
        if s is not None and status in (200, 404):
            ttl = cache_hours if status == 200 else min(cache_hours, 1)
            vals = dict(key=key, status=status, body=body, fetched_at=now(), expires_at=now() + timedelta(hours=ttl))
            s.execute(insert(ApiCache).values(**vals).on_conflict_do_update(
                index_elements=[ApiCache.key], set_={k: v for k, v in vals.items() if k != "key"}))
        if status == 403:
            raise ApiError("Blizzard refused the request (403). Check the API client's access.")
        return status, body

    # ------------------------------------------------------------------ capabilities
    def realm_for(self, ed: Edition, ch_ruleset: str | None, ch_realm: str | None) -> str:
        if ed.uses_realms:
            if not ch_realm:
                raise Unsupported("A realm is required for this edition.")
            return slug(ch_realm)
        rs = ed.ruleset_realm_slugs.get(ch_ruleset or "", "")
        if not rs:
            raise Unsupported(f"{ed.name} has no realms, and the API realm slug for the "
                              f"{(ch_ruleset or '').upper()} ruleset hasn't been confirmed yet.")
        return rs

    def lookup_character(self, s: Session, ch: Character) -> CharacterMatch:
        ed = self.cfg.edition(ch.edition)
        ns = self.require(ed, "character", "profile", ch.region)
        realm = self.realm_for(ed, ch.ruleset, ch.realm_slug)
        path = f"/profile/wow/character/{quote(realm)}/{quote(ch.name.lower())}"
        status, body = self.get(s, ch.region, path, ns, self.bz.get("cache_hours_profile", 6))
        if status == 404:
            return CharacterMatch(False, "Blizzard has no public profile for that name (it may be new, "
                                         "private, or misspelled).", namespace=ns)
        m = CharacterMatch(True, "Blizzard returned a character with this name.", level=body.get("level"),
                           race=(body.get("race") or {}).get("name"),
                           klass=(body.get("character_class") or {}).get("name"),
                           faction=((body.get("faction") or {}).get("type") or "").lower() or None,
                           external_id=str(body.get("id")) if body.get("id") else None, namespace=ns)
        return m

    def apply_lookup(self, s: Session, ch: Character) -> CharacterMatch:
        """Look up and record the result on the character. Never sets ownership_verified."""
        m = self.lookup_character(s, ch)
        if m.found:
            if ch.verification != "ownership_verified":
                ch.verification = "api_matched"
            ch.api_external_id, ch.api_namespace, ch.api_refreshed_at = m.external_id, m.namespace, now()
            if m.level:
                ch.level = m.level
        return m

    def probe(self, s: Session, edition: str, region: str) -> dict[str, str]:
        """Check what Blizzard actually serves for an edition. Results go to api_probes."""
        ed = self.cfg.edition(edition)
        checks = {"reference": ("static", "/data/wow/playable-class/index"),
                  "character": ("dynamic", "/data/wow/realm/index"),
                  "quests": ("static", "/data/wow/quest/area/index"),
                  "locations": ("static", "/data/wow/journal-instance/index")}
        out = {}
        for cap, (kind, path) in checks.items():
            ns = ed.namespace(kind, region)
            if not ns:
                res, det = "unsupported", "no namespace configured (not published by Blizzard)"
            else:
                try:
                    st, _ = self.get(None, region, path, ns, 0)
                    res, det = ("ok", f"{ns} {path} -> 200") if st == 200 else (f"http_{st}", f"{ns} {path}")
                except (ApiError, Exception) as exc:  # noqa: BLE001 - probe reports everything
                    res, det = "error", str(exc)[:400]
            out[cap] = res
            vals = dict(edition=edition, region=region, capability=cap, result=res, detail=det, probed_at=now())
            s.execute(insert(ApiProbe).values(**vals).on_conflict_do_update(
                index_elements=[ApiProbe.edition, ApiProbe.region, ApiProbe.capability],
                set_={k: v for k, v in vals.items() if k not in ("edition", "region", "capability")}))
        out["ownership"] = "unsupported" if not ed.namespace("profile", region) else "needs_oauth"
        return out

    def import_reference(self, s: Session, edition: str, region: str) -> dict[str, int]:
        """Classes, races, quest areas, dungeon/raid instances. Rows marked source=blizzard, verified."""
        ed = self.cfg.edition(edition)
        ns = self.require(ed, "reference", "static", region)
        hours = self.bz.get("cache_hours_static", 168)
        counts = {}
        plan = [("class", "/data/wow/playable-class/index", "classes"),
                ("race", "/data/wow/playable-race/index", "races")]
        if ed.capability("quests") != "unsupported":
            plan.append(("zone", "/data/wow/quest/area/index", "areas"))
        if ed.capability("locations") != "unsupported":
            plan.append(("location", "/data/wow/journal-instance/index", "instances"))
        for kind, path, field in plan:
            st, body = self.get(s, region, path, ns, hours)
            if st != 200:
                counts[kind] = 0
                continue
            n = 0
            for item in body.get(field, []):
                name = item.get("name")
                if isinstance(name, dict):
                    name = name.get("en_US")
                if not name:
                    continue
                key = slug(name).replace("-", "_")
                vals = dict(edition=edition, kind=kind, key=key, label=name[:160],
                            parent_key="" if kind != "race" else "", source="blizzard",
                            source_ref=f"{ns}{path}", external_id=str(item.get("id", "")),
                            verification="verified", data={}, updated_at=now())
                s.execute(insert(ReferenceOption).values(**vals).on_conflict_do_update(
                    constraint="ux_ref_key", set_={k: v for k, v in vals.items()
                                                   if k in ("label", "source", "source_ref", "external_id",
                                                            "verification", "updated_at")}))
                n += 1
            counts[kind] = n
        return counts

    # ------------------------------------------------------------------ optional ownership (Battle.net login)
    def authorize_url(self, region: str, redirect_uri: str) -> tuple[str, str]:
        if not self.bz.get("battlenet_link_enabled"):
            raise Unsupported("Battle.net account linking is turned off.")
        state = pysecrets.token_urlsafe(24)
        q = urlencode({"client_id": get_secret("blizzard-client-id").reveal(), "scope": "wow.profile",
                       "state": state, "redirect_uri": redirect_uri, "response_type": "code"})
        return f"{self.bz['oauth_base'].rstrip('/')}/authorize?{q}", state

    def link_account(self, s: Session, member_id: int, edition: str, region: str, code: str,
                     redirect_uri: str) -> int:
        """Exchange the code, read the account's character list once, mark matching characters verified.

        The user token is never stored (Battle.net user tokens last ~24h and have no refresh token).
        """
        ed = self.cfg.edition(edition)
        ns = self.require(ed, "ownership", "profile", region)
        r = self._send("POST", f"{self.bz['oauth_base'].rstrip('/')}/token",
                       auth=(get_secret("blizzard-client-id").reveal(), get_secret("blizzard-client-secret").reveal()),
                       data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
        if r.status_code != 200:
            raise ApiError("Battle.net login failed.")
        user_token = r.json()["access_token"]
        url = self.cfg["blizzard"]["api_host_template"].replace("{region}", region) + "/profile/user/wow"
        r2 = self._send("GET", url, params={"namespace": ns, "locale": "en_US"},
                        headers={"Authorization": f"Bearer {user_token}"})
        del user_token
        if r2.status_code != 200:
            raise ApiError(f"Couldn't read the Battle.net account ({r2.status_code}).")
        chars = []
        for acct in r2.json().get("wow_accounts", []):
            for c in acct.get("characters", []):
                chars.append({"id": str(c.get("id")), "name": c.get("name", "").lower(),
                              "realm": (c.get("realm") or {}).get("slug", "")})
        key = get_secret("field-key")
        blob = Fernet(key.reveal().encode()).encrypt(json.dumps(chars).encode())
        vals = dict(member_id=member_id, region=region, snapshot_enc=blob, linked_at=now())
        s.execute(insert(BattlenetLink).values(**vals).on_conflict_do_update(
            index_elements=[BattlenetLink.member_id, BattlenetLink.region],
            set_={"snapshot_enc": blob, "linked_at": now()}))
        n = 0
        mine = s.scalars(select(Character).where(Character.member_id == member_id, Character.edition == edition,
                                                 Character.region == region, Character.archived_at.is_(None)))
        for ch in mine:
            try:
                realm = self.realm_for(ed, ch.ruleset, ch.realm_slug)
            except Unsupported:
                continue
            hit = next((c for c in chars if c["name"] == ch.name.lower() and c["realm"] == realm), None)
            if hit:
                ch.verification, ch.api_external_id, ch.api_refreshed_at = "ownership_verified", hit["id"], now()
                n += 1
        return n
