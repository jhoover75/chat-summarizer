"""
main.py — CLI entry point for chat-summarizer.

Normal run pipeline:
  1.  Load config + init Postgres
  2.  Load state (last-fetched timestamp per channel)
  3.  Flag threads idle > thread_age_out_days as 'aged_out'
  4.  For each source / channel:
        a. Resolve first-run lookback if no prior state
        b. Fetch all channel messages into memory (no DB write)
        c. Discover eligible threads (started / following / mentioned / replied)
        d. For newly-discovered threads: backfill full thread history into Postgres
        e. For already-tracked threads: store only the new messages
        f. Upsert tracked_threads + update last_activity
        g. Update state timestamp
  5.  For every active thread with needs_resummary=TRUE:
        a. Load full thread from Postgres (source of truth)
        b. Append new messages to raw.md (cumulative transcript)
        c. Re-summarize from scratch → overwrite summary.md
        d. Mark needs_resummary=FALSE in Postgres
  6.  Save state

Prune mode (--prune, must be run interactively):
  1.  Flag aged-out threads
  2.  Show list with confirmation prompts
  3.  Delete approved threads from Postgres + disk

See DESIGN.md §5 for the complete data flow description.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import structlog

from src.config import load_config, MeConfig
from src.archive.db import (
    init_db,
    upsert_messages,
    upsert_tracked_thread,
    get_tracked_threads,
    update_thread_activity,
    mark_summarized,
    flag_aged_out_threads,
    get_aged_out_threads,
    delete_thread_data,
    get_thread_messages,
    get_channel_state,
    upsert_channel_state,
)
from src.archive.models import TrackedThread
from src.sources.rocketchat import RocketChatSource
from src.sources.teams import TeamsSource
from src.summarizer.ollama_client import OllamaClient
from src.summarizer.prompt_builder import build_thread_prompt
from src.output.markdown_writer import append_raw, write_summary
from src import utils

log = structlog.get_logger()


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _thread_dir(output_directory: str, thread: TrackedThread) -> Path:
    """
    Canonical output directory for a thread:
      <output_dir>/<platform>/<channel>/<YYYY-MM-DD>/<thread_id>/
    The date component is the day of thread origination (or today if unknown).
    """
    if thread.originated_at:
        date_str = thread.originated_at.strftime("%Y-%m-%d")
    elif thread.tracked_since:
        date_str = thread.tracked_since.strftime("%Y-%m-%d")
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    parts = [output_directory, thread.platform]
    if thread.team:
        parts.append(thread.team)
    parts += [thread.channel, date_str, thread.thread_id]
    return Path(*parts)


def _messages_for_thread(messages, thread_id: str):
    """Return the subset of in-memory messages that belong to a thread."""
    return [
        m for m in messages
        if m.thread_id == thread_id          # reply messages
        or (m.thread_id is None and m.message_id == thread_id)  # top-level post
    ]


def _channel_name(channel_config) -> str:
    return getattr(channel_config, "channel_name", None) or getattr(channel_config, "name", "")


# ── prune mode ────────────────────────────────────────────────────────────────

def run_prune(conn, config) -> int:
    """Interactive prune: review aged-out threads and delete on confirmation."""
    flagged = flag_aged_out_threads(conn, config.tracking.thread_age_out_days)
    log.info("prune_flagged", newly_flagged=flagged)

    aged = get_aged_out_threads(conn)
    if not aged:
        print("No aged-out threads found. Nothing to prune.")
        return 0

    print(f"\nFound {len(aged)} aged-out thread(s):\n")
    pruned = 0

    for i, thread in enumerate(aged, 1):
        last_seen = thread.last_activity.strftime("%Y-%m-%d") if thread.last_activity else "unknown"
        print(f"  [{i}/{len(aged)}] {thread.platform} / {thread.channel} / {thread.thread_id}")
        print(f"         Subject : {thread.thread_subject or '(no subject)'}")
        print(f"         URL     : {thread.thread_url or '(no URL)'}")
        print(f"         Reason  : {thread.reason}")
        print(f"         Last activity: {last_seen}")
        print()

        try:
            answer = input("  Delete this thread and all its data? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            break

        if answer == "y":
            delete_thread_data(conn, thread.platform, thread.channel, thread.thread_id)
            # Remove output directory if it exists
            tdir = _thread_dir(config.output.directory, thread)
            if tdir.exists():
                shutil.rmtree(tdir)
                log.info("prune_deleted_dir", path=str(tdir))
            log.info("prune_deleted_thread", thread_id=thread.thread_id)
            pruned += 1
            print("  → Deleted.\n")
        else:
            print("  → Skipped.\n")

    print(f"Pruning complete. {pruned}/{len(aged)} thread(s) deleted.")
    return 0


# ── normal run ────────────────────────────────────────────────────────────────

def run_normal(conn, config, run_id: str) -> int:
    # Step 3 — age out stale threads before doing anything else
    newly_aged = flag_aged_out_threads(conn, config.tracking.thread_age_out_days)
    if newly_aged:
        log.info("aged_out_flagged", count=newly_aged)

    sources = []
    if config.rocketchat.enabled:
        sources.append(RocketChatSource(config.rocketchat))
    if config.teams.enabled:
        sources.append(TeamsSource(config.teams))

    failed_channels: list[str] = []

    # Step 4 — per-channel fetch + thread discovery
    for source in sources:
        # Index already-tracked threads for this platform (any status) keyed by thread_id
        existing_threads: dict[str, TrackedThread] = {
            t.thread_id: t
            for t in get_tracked_threads(conn, platform=source.PLATFORM, status=None)
        }

        for channel_config in source.channels():
            key = channel_config.state_key
            log.info("channel_start", key=key)

            try:
                since_ts = get_channel_state(conn, key)
                if since_ts is None:
                    # First run: look back first_run_lookback_days
                    lookback_dt = datetime.now(timezone.utc) - timedelta(
                        days=config.tracking.first_run_lookback_days
                    )
                    since_ts = lookback_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    log.info("first_run_lookback", key=key, since=since_ts)

                # 4b — in-memory fetch (no DB writes yet)
                messages = source.fetch_all(channel_config, since_ts)
                if not messages:
                    log.info("channel_no_messages", key=key)
                    continue

                # 4c — platform-native following + eligibility detection
                followed_ids = source.fetch_followed_threads(channel_config)
                candidates = source.discover_eligible_threads(
                    channel_config, messages, config.me, followed_ids
                )

                log.info(
                    "channel_discovered",
                    key=key,
                    messages=len(messages),
                    eligible_threads=len(candidates),
                )

                for thread in candidates:
                    thread_messages_to_store = []

                    if thread.thread_id not in existing_threads:
                        # 4d — first discovery: backfill full thread history
                        log.info("thread_backfill", thread_id=thread.thread_id)
                        thread_messages_to_store = source.fetch_thread_messages(
                            channel_config, thread.thread_id
                        )
                    else:
                        # 4e — already tracked: store only new messages from this window
                        thread_messages_to_store = _messages_for_thread(
                            messages, thread.thread_id
                        )

                    if thread_messages_to_store:
                        upsert_messages(conn, thread_messages_to_store)

                    # 4f — upsert tracked_threads record
                    upsert_tracked_thread(conn, thread)

                    # Update last_activity if new messages arrived
                    window_msgs = _messages_for_thread(messages, thread.thread_id)
                    if window_msgs:
                        latest = max(m.timestamp for m in window_msgs)
                        latest_dt = _parse_ts(latest)
                        if latest_dt:
                            update_thread_activity(
                                conn,
                                thread.platform,
                                thread.channel,
                                thread.thread_id,
                                latest_dt,
                            )

                # 4g — advance the per-channel cursor in Postgres
                upsert_channel_state(conn, key, messages[-1].timestamp)
                log.info("channel_done", key=key)

            except Exception as exc:
                log.error("channel_failed", key=key, error=str(exc), exc_info=True)
                failed_channels.append(key)

    # Step 5 — summarize threads that need it
    ollama = OllamaClient(config.ollama)

    active_threads = get_tracked_threads(conn, status="active")
    for thread in active_threads:
        if not thread.needs_resummary:
            continue

        log.info("thread_summarize_start", thread_id=thread.thread_id)
        try:
            all_msgs = get_thread_messages(
                conn, thread.platform, thread.channel, thread.thread_id
            )
            if not all_msgs:
                log.warning("thread_no_messages", thread_id=thread.thread_id)
                continue

            out_dir = _thread_dir(config.output.directory, thread)

            # 5b — append new messages to raw.md
            # "New" = messages not yet appended = those arriving in this run's window
            # We use the in-memory `messages` from the channel fetch. If thread
            # was just backfilled, all messages are "new" for raw.md purposes.
            new_msgs = _messages_for_thread_from_db_on_backfill(
                all_msgs, out_dir
            )
            if new_msgs:
                append_raw(thread, new_msgs, out_dir)

            # 5c — re-summarize from full Postgres record → overwrite summary.md
            prompt = build_thread_prompt(config.ollama, thread, all_msgs)
            summary_text = ollama._generate(prompt)
            write_summary(thread, summary_text, out_dir)

            # 5d — clear the flag
            mark_summarized(conn, thread.platform, thread.channel, thread.thread_id)
            log.info("thread_summarized", thread_id=thread.thread_id, messages=len(all_msgs))

        except Exception as exc:
            log.error(
                "thread_summarize_failed",
                thread_id=thread.thread_id,
                error=str(exc),
                exc_info=True,
            )

    conn.close()
    log.info("run_complete", run_id=run_id, failed=failed_channels)
    return 1 if failed_channels else 0


def _messages_for_thread_from_db_on_backfill(all_msgs, out_dir: Path):
    """
    Return only the messages not yet present in raw.md.
    Reads the existing raw.md (if any) and finds the last-appended timestamp marker,
    then returns messages newer than that. On first write, returns all messages.
    """
    raw_path = out_dir / "raw.md"
    if not raw_path.exists():
        return all_msgs

    content = raw_path.read_text(encoding="utf-8")
    # Find the most recent <!-- appended TIMESTAMP --> marker
    import re
    markers = re.findall(r"<!-- appended ([^-]+?) -->", content)
    if not markers:
        return all_msgs  # File exists but no markers — treat as fresh

    last_marker_ts = markers[-1].strip()
    try:
        last_dt = datetime.fromisoformat(last_marker_ts.replace("Z", "+00:00"))
    except ValueError:
        return all_msgs

    return [m for m in all_msgs if _parse_ts(m.timestamp) and _parse_ts(m.timestamp) > last_dt]


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="chat-summarizer — track and summarize chat threads with a local LLM"
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Interactive mode: review and permanently delete aged-out threads. "
            "Must be run with a TTY (docker run -it). Never pass to cron jobs."
        ),
    )
    args = parser.parse_args()

    config = load_config(args.config)
    utils.logging.configure(config.logging)

    run_id = str(uuid.uuid4())
    log.info("run_start", run_id=run_id, prune=args.prune)

    conn = init_db(config.archive)

    if args.prune:
        rc = run_prune(conn, config)
        conn.close()
        return rc

    return run_normal(conn, config, run_id)


if __name__ == "__main__":
    sys.exit(main())
