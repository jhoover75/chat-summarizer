"""
Tests for the PostgreSQL archive layer: upsert idempotency, prune, schema.

These tests require a live PostgreSQL instance. The connection parameters are
read from environment variables (same as the app). Run with:

    POSTGRES_HOST=localhost POSTGRES_DB=test_chat_summarizer \
    POSTGRES_USER=summarizer POSTGRES_PASSWORD=test \
    pytest tests/test_archive.py

For CI, spin up a Postgres container first:
    docker run -d -e POSTGRES_DB=test_chat_summarizer \
        -e POSTGRES_USER=summarizer -e POSTGRES_PASSWORD=test \
        -p 5432:5432 postgres:16-alpine
"""

import os
from datetime import datetime, timezone, timedelta

import pytest
import psycopg2

from src.archive.db import (
    init_db,
    upsert_messages,
    prune_old_messages,
    upsert_tracked_thread,
    get_tracked_threads,
    update_thread_activity,
    mark_summarized,
    flag_aged_out_threads,
    get_aged_out_threads,
    delete_thread_data,
    get_thread_messages,
)
from src.archive.models import Message, TrackedThread
from src.config import ArchiveConfig


def _test_config() -> ArchiveConfig:
    return ArchiveConfig(
        enabled=True,
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "test_chat_summarizer"),
        user=os.environ.get("POSTGRES_USER", "summarizer"),
        password=os.environ.get("POSTGRES_PASSWORD", "test"),
        retention_days=90,
    )


def make_message(msg_id: str = "msg1", platform: str = "rocketchat") -> Message:
    return Message(
        message_id=msg_id,
        platform=platform,
        channel="general",
        team=None,
        author_id="u1",
        author_name="alice",
        body="Hello world",
        timestamp="2026-07-05T09:00:00Z",
        thread_id=None,
        raw_json="{}",
    )


def make_thread(
    thread_id: str = "t1",
    platform: str = "rocketchat",
    channel: str = "general",
    reason: str = "started",
    **kwargs,
) -> TrackedThread:
    now = datetime.now(timezone.utc)
    return TrackedThread(
        platform=platform,
        channel=channel,
        team=None,
        thread_id=thread_id,
        thread_subject="Hello thread",
        thread_url=f"https://chat.example.com/channel/{channel}?tmid={thread_id}",
        originated_at=now - timedelta(days=1),
        tracked_since=now,
        last_activity=now,
        reason=reason,
        status="active",
        needs_resummary=True,
        **kwargs,
    )


@pytest.fixture
def db():
    """Create a fresh connection and clean up all test data after each test."""
    cfg = _test_config()
    try:
        conn = init_db(cfg)
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL not available: {e}")

    yield conn

    # Teardown: remove test data
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tracked_threads")
        cur.execute("DELETE FROM summaries")
        cur.execute("DELETE FROM messages")
    conn.commit()
    conn.close()


def test_upsert_idempotent(db):
    msg = make_message()
    count1 = upsert_messages(db, [msg])
    count2 = upsert_messages(db, [msg])  # duplicate — ON CONFLICT DO NOTHING
    assert count1 == 1
    assert count2 == 0


def test_upsert_multiple_platforms(db):
    rc_msg = make_message("msg1", "rocketchat")
    teams_msg = make_message("msg1", "teams")  # same ID, different platform = different PK
    count = upsert_messages(db, [rc_msg, teams_msg])
    assert count == 2


def test_prune_old_messages(db):
    old_msg = Message(
        message_id="old1", platform="rocketchat", channel="general", team=None,
        author_id="u1", author_name="alice", body="old", thread_id=None, raw_json="{}",
        timestamp="2020-01-01T00:00:00Z",
    )
    upsert_messages(db, [old_msg])
    pruned = prune_old_messages(db, retention_days=1)
    assert pruned == 1


def test_upsert_preserves_raw_json(db):
    msg = make_message()
    msg.raw_json = '{"_id": "msg1", "u": {"name": "alice"}}'
    upsert_messages(db, [msg])

    with db.cursor() as cur:
        cur.execute(
            "SELECT raw_json FROM messages WHERE platform = %s AND message_id = %s",
            ("rocketchat", "msg1"),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0]["_id"] == "msg1"


# ── tracked_threads tests ─────────────────────────────────────────────────────

def test_upsert_tracked_thread_insert(db):
    """A new thread should be inserted with needs_resummary=TRUE."""
    thread = make_thread()
    upsert_tracked_thread(db, thread)

    rows = get_tracked_threads(db, platform="rocketchat", status="active")
    assert len(rows) == 1
    assert rows[0].thread_id == "t1"
    assert rows[0].needs_resummary is True
    assert rows[0].thread_url is not None


def test_upsert_tracked_thread_idempotent(db):
    """Upserting the same thread twice should not create a duplicate row."""
    thread = make_thread()
    upsert_tracked_thread(db, thread)
    upsert_tracked_thread(db, thread)

    rows = get_tracked_threads(db)
    assert len(rows) == 1


def test_upsert_tracked_thread_preserves_tracked_since(db):
    """tracked_since should not be overwritten by a subsequent upsert."""
    thread = make_thread()
    upsert_tracked_thread(db, thread)

    rows = get_tracked_threads(db)
    original_tracked_since = rows[0].tracked_since

    # Upsert again — tracked_since must be unchanged
    upsert_tracked_thread(db, thread)
    rows2 = get_tracked_threads(db)
    assert rows2[0].tracked_since == original_tracked_since


def test_needs_resummary_flag_lifecycle(db):
    """needs_resummary should start TRUE and be cleared by mark_summarized()."""
    thread = make_thread()
    upsert_tracked_thread(db, thread)

    rows = get_tracked_threads(db, status="active")
    assert rows[0].needs_resummary is True

    mark_summarized(db, "rocketchat", "general", "t1")

    rows2 = get_tracked_threads(db, status="active")
    assert rows2[0].needs_resummary is False


def test_update_thread_activity_sets_needs_resummary(db):
    """update_thread_activity() should set needs_resummary=TRUE."""
    thread = make_thread()
    upsert_tracked_thread(db, thread)
    mark_summarized(db, "rocketchat", "general", "t1")

    # Simulate new activity
    new_activity = datetime.now(timezone.utc) + timedelta(hours=1)
    update_thread_activity(db, "rocketchat", "general", "t1", new_activity)

    rows = get_tracked_threads(db, status="active")
    assert rows[0].needs_resummary is True
    assert rows[0].last_activity >= new_activity.replace(microsecond=0)


def test_thread_url_stored_and_retrieved(db):
    """thread_url should round-trip through Postgres unchanged."""
    thread = make_thread(thread_url="https://example.com/channel/general?tmid=t1")
    upsert_tracked_thread(db, thread)

    rows = get_tracked_threads(db)
    assert rows[0].thread_url == "https://example.com/channel/general?tmid=t1"


def test_flag_aged_out_threads(db):
    """Threads with no recent activity should be flagged aged_out."""
    now = datetime.now(timezone.utc)
    old_thread = make_thread(thread_id="old_t")
    old_thread.last_activity = now - timedelta(days=100)
    upsert_tracked_thread(db, old_thread)

    fresh_thread = make_thread(thread_id="fresh_t")
    fresh_thread.last_activity = now
    upsert_tracked_thread(db, fresh_thread)

    flagged = flag_aged_out_threads(db, age_out_days=90)
    assert flagged == 1

    aged = get_aged_out_threads(db)
    assert len(aged) == 1
    assert aged[0].thread_id == "old_t"
    assert aged[0].status == "aged_out"

    active = get_tracked_threads(db, status="active")
    assert len(active) == 1
    assert active[0].thread_id == "fresh_t"


def test_delete_thread_data(db):
    """delete_thread_data() should remove both tracked_threads and messages rows."""
    thread = make_thread(thread_id="del_t")
    upsert_tracked_thread(db, thread)

    msg = make_message(msg_id="del_msg")
    msg.thread_id = "del_t"
    upsert_messages(db, [msg])

    delete_thread_data(db, "rocketchat", "general", "del_t")

    rows = get_tracked_threads(db)
    assert not any(r.thread_id == "del_t" for r in rows)

    msgs = get_thread_messages(db, "rocketchat", "general", "del_t")
    assert msgs == []


def test_get_thread_messages_ordered(db):
    """get_thread_messages() should return messages sorted by timestamp ascending."""
    thread = make_thread(thread_id="tmsg")
    upsert_tracked_thread(db, thread)

    msgs = [
        Message("m3", "rocketchat", "general", None, "u1", "alice",
                "third", "2026-07-05T11:00:00Z", "tmsg", "{}"),
        Message("m1", "rocketchat", "general", None, "u1", "alice",
                "first", "2026-07-05T09:00:00Z", "tmsg", "{}"),
        Message("m2", "rocketchat", "general", None, "u1", "alice",
                "second", "2026-07-05T10:00:00Z", "tmsg", "{}"),
    ]
    upsert_messages(db, msgs)

    retrieved = get_thread_messages(db, "rocketchat", "general", "tmsg")
    assert len(retrieved) == 3
    assert retrieved[0].timestamp < retrieved[1].timestamp < retrieved[2].timestamp
    assert retrieved[0].body == "first"
