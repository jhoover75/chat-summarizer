"""
db.py — PostgreSQL archive: connection, schema, and data access helpers.

Schema is applied idempotently on every startup via CREATE TABLE IF NOT EXISTS.
INSERT ... ON CONFLICT (platform, message_id) DO NOTHING ensures archival is idempotent.

psycopg2 uses %s placeholders (not ? as in sqlite3).

See DESIGN.md §12 for the full schema DDL with column-level comments.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras

from src.archive.models import Message, Summary, TrackedThread

SCHEMA_VERSION = 1

DDL = """
-- messages — raw message archive
CREATE TABLE IF NOT EXISTS messages (
    message_id      TEXT            NOT NULL,
    platform        TEXT            NOT NULL CHECK (platform IN ('rocketchat', 'teams')),
    channel         TEXT            NOT NULL,
    team            TEXT,
    author_id       TEXT            NOT NULL,
    author_name     TEXT            NOT NULL,
    body            TEXT            NOT NULL DEFAULT '',
    timestamp       TEXT            NOT NULL,
    thread_id       TEXT,
    raw_json        JSONB           NOT NULL DEFAULT '{}',
    archived_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (platform, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_platform_channel_ts
    ON messages (platform, channel, timestamp);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages (platform, thread_id)
    WHERE thread_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_ts
    ON messages (timestamp);

CREATE INDEX IF NOT EXISTS idx_messages_raw_json
    ON messages USING GIN (raw_json);

-- summaries — record of every generated summary
CREATE TABLE IF NOT EXISTS summaries (
    id                  BIGSERIAL       PRIMARY KEY,
    run_id              TEXT            NOT NULL,
    platform            TEXT            NOT NULL CHECK (platform IN ('rocketchat', 'teams')),
    channel             TEXT            NOT NULL,
    team                TEXT,
    summary_date        DATE            NOT NULL,
    message_count       INTEGER         NOT NULL DEFAULT 0,
    summary_text        TEXT            NOT NULL DEFAULT '',
    structured_json     JSONB           NOT NULL DEFAULT '{}',
    message_ids         JSONB           NOT NULL DEFAULT '[]',
    output_path         TEXT,
    model               TEXT            NOT NULL,
    generation_seconds  REAL,
    success             BOOLEAN         NOT NULL DEFAULT TRUE,
    error_message       TEXT,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_summaries_platform_channel_date
    ON summaries (platform, channel, summary_date);

CREATE INDEX IF NOT EXISTS idx_summaries_run_id
    ON summaries (run_id);

-- tracked_threads — threads the user cares about; one row per thread
CREATE TABLE IF NOT EXISTS tracked_threads (
    id              BIGSERIAL       PRIMARY KEY,
    platform        TEXT            NOT NULL CHECK (platform IN ('rocketchat', 'teams')),
    channel         TEXT            NOT NULL,
    team            TEXT,
    thread_id       TEXT            NOT NULL,
    thread_subject  TEXT,
    thread_url      TEXT,
    originated_at   TIMESTAMPTZ,
    tracked_since   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_activity   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    reason          TEXT            NOT NULL CHECK (reason IN ('started', 'following', 'mentioned', 'replied')),
    status          TEXT            NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'aged_out')),
    needs_resummary BOOLEAN         NOT NULL DEFAULT TRUE,
    UNIQUE (platform, channel, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_tracked_threads_status
    ON tracked_threads (status);

CREATE INDEX IF NOT EXISTS idx_tracked_threads_last_activity
    ON tracked_threads (last_activity);

CREATE INDEX IF NOT EXISTS idx_tracked_threads_needs_resummary
    ON tracked_threads (needs_resummary)
    WHERE needs_resummary = TRUE;

-- channel_state — last-fetched cursor per channel (replaces state.json)
-- state_key matches the channel_config.state_key format, e.g. "rocketchat::general"
-- or "teams::Engineering::general". One row per monitored channel.
CREATE TABLE IF NOT EXISTS channel_state (
    state_key       TEXT        PRIMARY KEY,
    last_fetched_at TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- schema_migrations — tracks applied migrations
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER         PRIMARY KEY,
    applied_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    description     TEXT
);

INSERT INTO schema_migrations (version, description)
    VALUES (1, 'Initial schema: messages, summaries, tracked_threads, channel_state tables')
    ON CONFLICT (version) DO NOTHING;
"""


def init_db(archive_config) -> psycopg2.extensions.connection:
    """Open a psycopg2 connection and apply the schema."""
    conn = psycopg2.connect(
        host=archive_config.host,
        port=archive_config.port,
        dbname=archive_config.database,
        user=archive_config.user,
        password=archive_config.password,
    )
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    return conn


def upsert_messages(conn: psycopg2.extensions.connection, messages: list[Message]) -> int:
    """
    Insert messages, silently skipping duplicates via ON CONFLICT DO NOTHING.
    Returns the count of newly inserted rows.
    """
    inserted = 0
    with conn.cursor() as cur:
        for msg in messages:
            # Parse raw_json string to dict so psycopg2 can serialise as JSONB
            try:
                raw = json.loads(msg.raw_json) if isinstance(msg.raw_json, str) else msg.raw_json
            except (json.JSONDecodeError, TypeError):
                raw = {}

            cur.execute(
                """
                INSERT INTO messages
                    (message_id, platform, channel, team, author_id, author_name,
                     body, timestamp, thread_id, raw_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (platform, message_id) DO NOTHING
                """,
                (
                    msg.message_id, msg.platform, msg.channel, msg.team,
                    msg.author_id, msg.author_name, msg.body, msg.timestamp,
                    msg.thread_id, json.dumps(raw),
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def insert_summary(conn: psycopg2.extensions.connection, summary: Summary) -> None:
    """Record a completed (or failed) summarization run."""
    structured = json.dumps({
        "key_points": summary.key_points,
        "actions": summary.actions,
    })
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO summaries
                (run_id, platform, channel, team, summary_date, message_count,
                 summary_text, structured_json, message_ids, output_path,
                 model, generation_seconds, success, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                summary.run_id, summary.platform, summary.channel, summary.team,
                summary.summary_date, summary.message_count, summary.summary_text,
                structured, json.dumps(summary.message_ids), summary.output_path,
                summary.model, summary.generation_seconds,
                summary.success, summary.error_message,
            ),
        )
    conn.commit()


def get_message(
    conn: psycopg2.extensions.connection, platform: str, message_id: str
) -> Optional[Message]:
    """Fetch a single message from the archive by platform + message_id."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM messages WHERE platform = %s AND message_id = %s",
            (platform, message_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Message(
        message_id=row["message_id"],
        platform=row["platform"],
        channel=row["channel"],
        team=row["team"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        body=row["body"],
        timestamp=row["timestamp"],
        thread_id=row["thread_id"],
        raw_json=json.dumps(row["raw_json"]),
    )


def prune_old_messages(
    conn: psycopg2.extensions.connection, retention_days: int
) -> int:
    """Delete messages older than retention_days. Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages WHERE timestamp < %s", (cutoff,))
        deleted = cur.rowcount
    conn.commit()
    return deleted


# ── channel_state helpers ─────────────────────────────────────────────────────

def get_channel_state(
    conn: psycopg2.extensions.connection, state_key: str
) -> Optional[str]:
    """
    Return the last-fetched ISO timestamp for a channel, or None if this channel
    has never been fetched before (i.e. first run).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_fetched_at FROM channel_state WHERE state_key = %s",
            (state_key,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    # Return as an ISO 8601 string with UTC suffix, consistent with prior state.json format
    ts: datetime = row[0]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_channel_state(
    conn: psycopg2.extensions.connection, state_key: str, last_fetched_at: str
) -> None:
    """
    Record the timestamp of the most recent message fetched for a channel.
    Called at the end of each successful channel sweep.
    last_fetched_at should be an ISO 8601 string (e.g. "2026-07-05T09:00:00Z").
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO channel_state (state_key, last_fetched_at, updated_at)
            VALUES (%s, %s::TIMESTAMPTZ, NOW())
            ON CONFLICT (state_key) DO UPDATE SET
                last_fetched_at = EXCLUDED.last_fetched_at,
                updated_at      = NOW()
            """,
            (state_key, last_fetched_at),
        )
    conn.commit()


# ── tracked_threads helpers ───────────────────────────────────────────────────

def _row_to_tracked_thread(row: dict) -> TrackedThread:
    return TrackedThread(
        platform=row["platform"],
        channel=row["channel"],
        team=row["team"],
        thread_id=row["thread_id"],
        thread_subject=row["thread_subject"],
        thread_url=row["thread_url"],
        originated_at=row["originated_at"],
        tracked_since=row["tracked_since"],
        last_activity=row["last_activity"],
        reason=row["reason"],
        status=row["status"],
        needs_resummary=row["needs_resummary"],
    )


def upsert_tracked_thread(
    conn: psycopg2.extensions.connection, thread: TrackedThread
) -> None:
    """
    Insert a new tracked thread, or update non-identity fields if it already exists.

    tracked_since and reason are preserved from the original row on conflict.
    thread_url and thread_subject are updated if the new value is non-NULL.
    last_activity is set to the later of the existing and new value.
    needs_resummary is set to TRUE if the upsert brings in new activity.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tracked_threads
                (platform, channel, team, thread_id, thread_subject, thread_url,
                 originated_at, last_activity, reason, status, needs_resummary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (platform, channel, thread_id) DO UPDATE SET
                thread_subject  = COALESCE(EXCLUDED.thread_subject, tracked_threads.thread_subject),
                thread_url      = COALESCE(EXCLUDED.thread_url,     tracked_threads.thread_url),
                originated_at   = COALESCE(tracked_threads.originated_at, EXCLUDED.originated_at),
                last_activity   = GREATEST(tracked_threads.last_activity, EXCLUDED.last_activity),
                -- re-activate if it was aged out and new activity arrived
                status          = CASE
                                    WHEN EXCLUDED.last_activity > tracked_threads.last_activity
                                    THEN 'active'
                                    ELSE tracked_threads.status
                                  END,
                needs_resummary = TRUE
            """,
            (
                thread.platform,
                thread.channel,
                thread.team,
                thread.thread_id,
                thread.thread_subject,
                thread.thread_url,
                thread.originated_at,
                thread.last_activity,
                thread.reason,
                thread.status,
                thread.needs_resummary,
            ),
        )
    conn.commit()


def get_tracked_threads(
    conn: psycopg2.extensions.connection,
    platform: Optional[str] = None,
    status: Optional[str] = "active",
) -> list[TrackedThread]:
    """
    Return tracked threads, optionally filtered by platform and/or status.
    Pass status=None to return threads regardless of status.
    """
    conditions = []
    params: list = []

    if platform is not None:
        conditions.append("platform = %s")
        params.append(platform)
    if status is not None:
        conditions.append("status = %s")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM tracked_threads {where} ORDER BY last_activity DESC"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [_row_to_tracked_thread(dict(r)) for r in rows]


def update_thread_activity(
    conn: psycopg2.extensions.connection,
    platform: str,
    channel: str,
    thread_id: str,
    last_activity: datetime,
) -> None:
    """
    Mark that new messages arrived in a thread.
    Sets last_activity to the most recent message timestamp and needs_resummary=TRUE.
    Re-activates threads that were previously aged out.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tracked_threads
               SET last_activity   = GREATEST(last_activity, %s),
                   needs_resummary = TRUE,
                   status          = 'active'
             WHERE platform = %s AND channel = %s AND thread_id = %s
            """,
            (last_activity, platform, channel, thread_id),
        )
    conn.commit()


def mark_summarized(
    conn: psycopg2.extensions.connection,
    platform: str,
    channel: str,
    thread_id: str,
) -> None:
    """Clear the needs_resummary flag after summary.md has been written."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tracked_threads
               SET needs_resummary = FALSE
             WHERE platform = %s AND channel = %s AND thread_id = %s
            """,
            (platform, channel, thread_id),
        )
    conn.commit()


def flag_aged_out_threads(
    conn: psycopg2.extensions.connection, age_out_days: int
) -> int:
    """
    Mark active threads with no new activity for age_out_days as 'aged_out'.
    Returns the number of threads newly flagged.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE tracked_threads
               SET status = 'aged_out'
             WHERE status = 'active'
               AND last_activity < NOW() - (%s || ' days')::INTERVAL
            """,
            (str(age_out_days),),
        )
        flagged = cur.rowcount
    conn.commit()
    return flagged


def get_aged_out_threads(
    conn: psycopg2.extensions.connection,
) -> list[TrackedThread]:
    """Return all threads currently flagged as aged_out."""
    return get_tracked_threads(conn, platform=None, status="aged_out")


def delete_thread_data(
    conn: psycopg2.extensions.connection,
    platform: str,
    channel: str,
    thread_id: str,
) -> None:
    """
    Permanently delete a thread and all its archived messages.
    Called only by the interactive --prune command after user confirmation.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM messages WHERE platform = %s AND channel = %s AND thread_id = %s",
            (platform, channel, thread_id),
        )
        cur.execute(
            "DELETE FROM tracked_threads WHERE platform = %s AND channel = %s AND thread_id = %s",
            (platform, channel, thread_id),
        )
    conn.commit()


def get_thread_messages(
    conn: psycopg2.extensions.connection,
    platform: str,
    channel: str,
    thread_id: str,
) -> list[Message]:
    """
    Retrieve all archived messages for a thread, ordered by timestamp ascending.
    Used as the source-of-truth when regenerating summary.md.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM messages
             WHERE platform = %s AND channel = %s AND thread_id = %s
             ORDER BY timestamp ASC
            """,
            (platform, channel, thread_id),
        )
        rows = cur.fetchall()

    return [
        Message(
            message_id=r["message_id"],
            platform=r["platform"],
            channel=r["channel"],
            team=r["team"],
            author_id=r["author_id"],
            author_name=r["author_name"],
            body=r["body"],
            timestamp=r["timestamp"],
            thread_id=r["thread_id"],
            raw_json=json.dumps(r["raw_json"]),
        )
        for r in rows
    ]
