#!/usr/bin/env python3
"""
seed_rocketchat.py — populate the local Rocket.Chat test stack with fictional
users, channels, and threaded conversations.

This targets the `testing` Docker Compose profile in docker-compose.yml (see
README.md, "Testing with a local Rocket.Chat instance") and gives
src/sources/rocketchat.py real data to fetch: multi-user threads, @mentions,
a followed thread, and messages authored by the "me" test account — enough
to exercise every branch of discover_eligible_threads() (started / following
/ mentioned / replied).

Usage:
    docker compose --profile testing up -d mongo mongo-init-replica rocketchat
    # wait for "SERVER RUNNING" in: docker compose logs -f rocketchat
    python3 scripts/seed_rocketchat.py

Safe to re-run: users and channels are created idempotently (existing ones
are reused and missing members are invited). Re-running does add a fresh
copy of the conversations below each time, since Rocket.Chat has no public
"does this message already exist" check — if you want a clean slate instead,
reset the stack first with `docker compose --profile testing down -v`.

Requires: httpx, tenacity (both already in requirements.txt).
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# ── defaults ─────────────────────────────────────────────────────────────────
# docker-compose.yml fronts Rocket.Chat with the rocketchat-proxy TLS proxy
# on host port 3030 (see the `rocketchat-proxy` service and rocketchat's
# ROOT_URL). Override with --url if your setup differs. The proxy uses a
# self-signed certificate, hence verify=False on every httpx.Client below.
DEFAULT_URL = "https://localhost:3030"
ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"  # matches ADMIN_PASS in docker-compose.yml
PERSONA_PASSWORD = "TestPass123!456"  # 15 chars: satisfies this stack's default min-length-14 policy

# "testuser" matches the username used throughout README.md's "Testing with a
# local Rocket.Chat instance" walkthrough, so config.yaml's
# me.rocketchat.username can point at it unchanged.
PERSONAS = [
    {"username": "testuser", "name": "Taylor Reyes", "email": "testuser@aurora.test"},
    {"username": "alice.chen", "name": "Alice Chen", "email": "alice.chen@aurora.test"},
    {"username": "bob.martins", "name": "Bob Martins", "email": "bob.martins@aurora.test"},
    {"username": "carla.diaz", "name": "Carla Diaz", "email": "carla.diaz@aurora.test"},
    {"username": "dev.patel", "name": "Dev Patel", "email": "dev.patel@aurora.test"},
    {"username": "erin.walsh", "name": "Erin Walsh", "email": "erin.walsh@aurora.test"},
    {"username": "felix.kim", "name": "Felix Kim", "email": "felix.kim@aurora.test"},
]
ALL_USERNAMES = [p["username"] for p in PERSONAS]

CHANNELS = ["general", "engineering", "incident-response", "product-launch", "random"]

# ── fictional conversations ─────────────────────────────────────────────────
# Each channel gets a list of entries. A "message" entry is a single
# standalone post; a "thread" entry is a root message plus replies (posted
# with tmid=<root message id>). Set follow_as on a thread to have that user
# call chat.followMessage on the root, exercising the platform-native
# "following" reason in discover_eligible_threads().
CONVERSATIONS: dict[str, list[dict]] = {
    "general": [
        {"type": "message", "user": "carla.diaz",
         "text": "Morning all! Coffee machine on the 3rd floor is finally fixed :coffee:"},
        {"type": "thread", "user": "carla.diaz",
         "text": ("Kicking off Q3 planning — I've put together a first pass at priorities "
                   "for the quarter. Big three: checkout performance, the new returns portal, "
                   "and shoring up on-call coverage. Thoughts before I circulate the doc widely?"),
         "replies": [
             {"user": "alice.chen",
              "text": "Returns portal has been requested by support for months, glad it's finally in scope. What's the rough timeline?"},
             {"user": "felix.kim",
              "text": "+1 on on-call coverage, we've been running thin since Marcus left."},
             {"user": "carla.diaz",
              "text": ("Aiming for returns portal beta by end of August, on-call hiring req is "
                        "already posted. @testuser can you sanity check the checkout performance "
                        "targets against what eng thinks is feasible?")},
             {"user": "testuser",
              "text": "Sure, I'll loop in with Alice and Bob today and get back to you by Thursday."},
             {"user": "carla.diaz",
              "text": "[DECISION] Let's treat Thursday as the checkpoint for scoping the quarter doc."},
         ]},
        {"type": "message", "user": "erin.walsh",
         "text": "Heads up — I'm out Friday for a dentist appointment, back Monday."},
        {"type": "thread", "user": "testuser",
         "text": ("Is anyone else having trouble with the VPN dropping every ~20 minutes today? "
                   "Started right after the network team's maintenance window."),
         "replies": [
             {"user": "dev.patel", "text": "Yeah, same here since about 9am. Filed it with IT as ticket #4471."},
             {"user": "alice.chen", "text": "Confirmed on my end too. Probably related to the firmware update they mentioned in the maintenance notice."},
             {"user": "dev.patel", "text": "IT says it's a known issue with the new VPN concentrator config and they're rolling back the change now."},
             {"user": "testuser", "text": "Good to know, thanks for the quick turnaround."},
         ]},
        {"type": "message", "user": "bob.martins",
         "text": "Reminder: pantry restock list is on the wiki, add anything you want ordered by Wednesday."},
    ],
    "engineering": [
        {"type": "message", "user": "alice.chen",
         "text": "Deployed the rate-limiter tweak to staging, looks stable so far."},
        {"type": "thread", "user": "bob.martins",
         "text": ("Found a nasty bug in the checkout service — if a cart has more than 25 line "
                   "items, the tax calculation silently returns $0.00 instead of erroring. Looks "
                   "like an off-by-one in the batch splitter."),
         "replies": [
             {"user": "alice.chen", "text": "Ouch. Do we know if this has hit production carts yet?"},
             {"user": "bob.martins",
              "text": ("Checked the logs — happened 14 times in the last 48 hours, all in "
                        "production. None of them completed checkout, so no bad orders went out, "
                        "but customers definitely saw $0 tax and probably got confused.")},
             {"user": "dev.patel",
              "text": "@testuser this feels like it should be a hotfix given the customer-facing impact, not a normal sprint ticket."},
             {"user": "testuser",
              "text": "Agreed, let's hotfix. Bob, can you have a patch up by end of day? I'll fast-track the review."},
             {"user": "bob.martins",
              "text": "[ACTION] On it — PR up within the hour, targeting a same-day deploy."},
             {"user": "alice.chen",
              "text": "[DECISION] We'll hotfix today and backfill a regression test for >25 line items so this can't slip through again."},
         ]},
        {"type": "thread", "user": "testuser",
         "text": ("Proposal: migrate our Postgres 14 instances to 16 next quarter to pick up the "
                   "new logical replication improvements before our compliance audit. Curious "
                   "about blockers."),
         "replies": [
             {"user": "bob.martins",
              "text": "Only blocker I can think of is the archive DB extension we vendor — need to confirm it's compatible with 16 first."},
             {"user": "alice.chen",
              "text": "I can check that this week. If it's compatible, I don't see why we couldn't start the migration plan in July."},
             {"user": "bob.martins",
              "text": "Sounds good, I'll spin up a test instance on 16 with a data snapshot to validate app compatibility in parallel."},
             {"user": "testuser",
              "text": "[DECISION] Great — let's target starting the migration the week of the 21st, contingent on the extension check."},
         ]},
        {"type": "message", "user": "felix.kim",
         "text": "PSA: staging is going to be flaky for the next hour, running a load test against the new caching layer."},
    ],
    "incident-response": [
        {"type": "thread", "user": "felix.kim",
         "text": ("P1: Payment gateway is returning 502s for ~15% of checkout attempts starting "
                   "14:02 UTC. Declaring an incident, I'll be IC."),
         "follow_as": "testuser",
         "replies": [
             {"user": "erin.walsh", "text": "On it — pulling up the gateway dashboards now."},
             {"user": "dev.patel", "text": "@testuser can you jump on the bridge? We may need eng to look at our retry logic too."},
             {"user": "testuser", "text": "Joining now."},
             {"user": "erin.walsh",
              "text": "Found it — the payment provider's status page shows a regional outage in us-east-2, matches our error spike exactly."},
             {"user": "felix.kim", "text": "Confirmed, it's on their end. Failing over our traffic to the us-west processor as a mitigation."},
             {"user": "dev.patel", "text": "Failover looks good, error rate dropping. Down to 2% now."},
             {"user": "felix.kim",
              "text": "[DECISION] Keeping failover in place until the provider confirms resolution. I'll post an all-clear once error rate is back to baseline for 30 min."},
             {"user": "erin.walsh", "text": "[ACTION] I'll write up the postmortem doc tomorrow and share for review."},
             {"user": "felix.kim",
              "text": "All clear — error rate back to baseline as of 15:10 UTC. Incident resolved, provider confirmed root cause on their side. Thanks everyone for the fast response."},
         ]},
        {"type": "message", "user": "erin.walsh",
         "text": "Postmortem draft for today's payment gateway incident is up for review."},
    ],
    "product-launch": [
        {"type": "thread", "user": "carla.diaz",
         "text": "Bad news — legal flagged a labeling requirement we hadn't accounted for in the EU rollout, which pushes our launch date.",
         "replies": [
             {"user": "alice.chen", "text": "How much slip are we talking?"},
             {"user": "carla.diaz",
              "text": "Legal thinks 2-3 weeks to get the updated packaging copy approved. So realistically we're looking at August 18th instead of August 1st."},
             {"user": "felix.kim",
              "text": "That's tight against the marketing campaign we already booked for Aug 1. Someone needs to talk to marketing today."},
             {"user": "carla.diaz", "text": "[ACTION] I'll talk to marketing this afternoon about shifting the campaign dates."},
             {"user": "alice.chen",
              "text": "[DECISION] Engineering will treat Aug 18 as the new target and can use the extra time to finish the last two launch-blocking bugs properly instead of rushing."},
         ]},
        {"type": "message", "user": "felix.kim",
         "text": "Updated launch checklist reflects the new Aug 18 date, link is in the channel topic."},
    ],
    "random": [
        {"type": "message", "user": "bob.martins",
         "text": "Anyone have a good ramen recommendation near the office? Craving something warm today."},
        {"type": "thread", "user": "erin.walsh",
         "text": "Team lunch this Friday to celebrate shipping the returns portal beta — thinking the Thai place on 5th. Good with everyone?",
         "replies": [
             {"user": "alice.chen", "text": "Works for me!"},
             {"user": "dev.patel", "text": "Same, count me in."},
             {"user": "felix.kim", "text": "Can we do 12:30 instead of noon? I have a call until then."},
             {"user": "erin.walsh", "text": "12:30 it is, I'll book a table for 7."},
         ]},
        {"type": "message", "user": "carla.diaz",
         "text": "Congrats to the team on the incident response yesterday — really smooth handling under pressure :clap:"},
    ],
}


def log(msg: str) -> None:
    print(f"[seed] {msg}", flush=True)


class RocketChatSeeder:
    def __init__(self, base_url: str, admin_password: str):
        self.base_url = base_url.rstrip("/")
        self.admin_password = admin_password
        self._anon = httpx.Client(base_url=self.base_url, timeout=30, verify=False)
        self.admin: httpx.Client | None = None
        # username -> authenticated httpx.Client (so posts are attributed to that user)
        self.persona_clients: dict[str, httpx.Client] = {}
        # username -> Rocket.Chat user _id
        self.persona_ids: dict[str, str] = {}
        # channel name -> roomId
        self.room_ids: dict[str, str] = {}

    # ── connection / auth ────────────────────────────────────────────────────

    def wait_ready(self, timeout: float = 120.0) -> None:
        log(f"Waiting for Rocket.Chat at {self.base_url} to become ready...")
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = self._anon.get("/api/info")
                if resp.status_code == 200:
                    log("Rocket.Chat is up.")
                    return
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(3)
        raise RuntimeError(
            f"Rocket.Chat did not become ready within {timeout:.0f}s at {self.base_url}"
        ) from last_error

    @retry(
        retry=retry_if_exception(lambda exc: RocketChatSeeder._rate_limited(exc)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(min=1, max=15),
    )
    def _login(self, username: str, password: str) -> httpx.Client:
        resp = self._anon.post(
            "/api/v1/login", json={"user": username, "password": password}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Login failed for {username}: {data}")
        auth = data["data"]
        return httpx.Client(
            base_url=self.base_url,
            headers={
                "X-Auth-Token": auth["authToken"],
                "X-User-Id": auth["userId"],
                "Content-Type": "application/json",
            },
            timeout=30,
            verify=False,
        )

    def login_admin(self) -> None:
        log(f"Logging in as admin ({ADMIN_USERNAME})...")
        self.admin = self._login(ADMIN_USERNAME, self.admin_password)

    # ── retry wrapper ────────────────────────────────────────────────────────

    @staticmethod
    def _rate_limited(exc: BaseException) -> bool:
        return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429

    @retry(
        retry=retry_if_exception(lambda exc: RocketChatSeeder._rate_limited(exc)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(min=1, max=15),
    )
    def _call(self, client: httpx.Client, method: str, path: str, **kwargs) -> dict:
        resp = client.request(method, path, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        if data.get("success") is False:
            raise RuntimeError(f"{method} {path} failed: {data}")
        time.sleep(0.25)  # keep well under Rocket.Chat's default rate limits
        return data

    # ── users ────────────────────────────────────────────────────────────────

    def ensure_user(self, persona: dict) -> str:
        username = persona["username"]
        assert self.admin is not None
        resp = self.admin.get("/api/v1/users.info", params={"username": username})
        if resp.status_code == 200 and resp.json().get("success"):
            user_id = resp.json()["user"]["_id"]
            log(f"User '{username}' already exists, resetting password...")
            # The account may pre-exist with an unknown password (e.g. created
            # manually per README, or from a run before PERSONA_PASSWORD
            # changed). Force it back to the known value so the login below
            # is guaranteed to succeed.
            self._call(
                self.admin,
                "POST",
                "/api/v1/users.update",
                json={"userId": user_id, "data": {"password": PERSONA_PASSWORD}},
            )
        else:
            log(f"Creating user '{username}'...")
            data = self._call(
                self.admin,
                "POST",
                "/api/v1/users.create",
                json={
                    "name": persona["name"],
                    "email": persona["email"],
                    "username": username,
                    "password": PERSONA_PASSWORD,
                    "active": True,
                    "verified": True,
                    "joinDefaultChannels": True,
                    "sendWelcomeEmail": False,
                    "roles": ["user"],
                },
            )
            user_id = data["user"]["_id"]
        self.persona_ids[username] = user_id
        self.persona_clients[username] = self._login(username, PERSONA_PASSWORD)
        return user_id

    # ── channels ─────────────────────────────────────────────────────────────

    def ensure_channel(self, name: str, members: list[str]) -> str:
        assert self.admin is not None
        resp = self.admin.get("/api/v1/channels.info", params={"roomName": name})
        if resp.status_code == 200 and resp.json().get("success"):
            room_id = resp.json()["channel"]["_id"]
            log(f"Channel '#{name}' already exists.")
        else:
            log(f"Creating channel '#{name}'...")
            data = self._call(
                self.admin,
                "POST",
                "/api/v1/channels.create",
                json={"name": name, "members": members},
            )
            room_id = data["channel"]["_id"]
        self.room_ids[name] = room_id

        # Make sure every persona is a member, in case the channel pre-existed
        # from a previous run without all of them.
        for username in members:
            invite_resp = self.admin.post(
                "/api/v1/channels.invite",
                json={"roomId": room_id, "userId": self.persona_ids[username]},
            )
            body = invite_resp.json()
            if not body.get("success") and body.get("errorType") not in (
                "error-user-already-in-room",
            ):
                log(f"  warning: could not invite {username} to #{name}: {body}")
        return room_id

    # ── messages ─────────────────────────────────────────────────────────────

    def post_message(self, username: str, channel: str, text: str, tmid: str | None = None) -> dict:
        client = self.persona_clients[username]
        # chat.postMessage's schema treats {channel, text} and {roomId, text,
        # tmid} as separate variants — mixing channel + tmid together fails
        # validation ("must match exactly one schema in oneOf"), so replies
        # must address the room by id instead of by channel name.
        if tmid:
            payload = {"roomId": self.room_ids[channel], "text": text, "tmid": tmid}
        else:
            payload = {"channel": f"#{channel}", "text": text}
        data = self._call(client, "POST", "/api/v1/chat.postMessage", json=payload)
        return data["message"]

    def follow_message(self, username: str, message_id: str) -> None:
        client = self.persona_clients[username]
        self._call(client, "POST", "/api/v1/chat.followMessage", json={"mid": message_id})

    # ── orchestration ────────────────────────────────────────────────────────

    def seed_conversations(self) -> None:
        for channel, entries in CONVERSATIONS.items():
            log(f"Posting conversations in #{channel}...")
            for entry in entries:
                if entry["type"] == "message":
                    self.post_message(entry["user"], channel, entry["text"])
                elif entry["type"] == "thread":
                    root = self.post_message(entry["user"], channel, entry["text"])
                    if entry.get("follow_as"):
                        self.follow_message(entry["follow_as"], root["_id"])
                    for reply in entry["replies"]:
                        self.post_message(reply["user"], channel, reply["text"], tmid=root["_id"])
                else:
                    raise ValueError(f"Unknown entry type: {entry['type']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Rocket.Chat base URL (default: {DEFAULT_URL})")
    parser.add_argument("--admin-password", default=DEFAULT_ADMIN_PASSWORD,
                         help="Admin account password (default: matches ADMIN_PASS in docker-compose.yml)")
    args = parser.parse_args()

    seeder = RocketChatSeeder(args.url, args.admin_password)
    try:
        seeder.wait_ready()
        seeder.login_admin()

        log(f"Ensuring {len(PERSONAS)} test users exist...")
        for persona in PERSONAS:
            seeder.ensure_user(persona)

        log(f"Ensuring {len(CHANNELS)} test channels exist...")
        for channel in CHANNELS:
            seeder.ensure_channel(channel, ALL_USERNAMES)

        seeder.seed_conversations()
    except (httpx.HTTPError, RuntimeError) as exc:
        log(f"ERROR: {exc}")
        return 1

    log("Done. Test users share the password: " + PERSONA_PASSWORD)
    log("The 'testuser' account matches README.md's documented me.rocketchat.username.")
    log(f"Log in at {args.url} as any of: {', '.join(ALL_USERNAMES)} (password '{PERSONA_PASSWORD}')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
