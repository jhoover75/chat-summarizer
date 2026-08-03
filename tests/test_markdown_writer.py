"""Tests for src/output/markdown_writer.py."""

from datetime import datetime, timezone

from src.archive.models import Message, TrackedThread
from src.output.markdown_writer import write_raw


def _thread(**overrides) -> TrackedThread:
    fields = dict(
        platform="rocketchat",
        channel="general",
        thread_id="root",
        reason="started",
        thread_subject="Original question",
        thread_url="https://chat.example.test/channel/general?tmid=root",
        tracked_since=datetime(2026, 7, 19, 21, 22, tzinfo=timezone.utc),
    )
    fields.update(overrides)
    return TrackedThread(**fields)


def _msg(message_id: str, body: str, timestamp: str) -> Message:
    return Message(
        message_id=message_id,
        platform="rocketchat",
        channel="general",
        team=None,
        author_id="u1",
        author_name="Alice",
        body=body,
        timestamp=timestamp,
        thread_id="root",
        raw_json="{}",
    )


def test_write_raw_contains_full_transcript(tmp_path):
    messages = [
        _msg("a", "How much slip are we talking?", "2026-07-19T21:22:00.000Z"),
        _msg("b", "Legal thinks 2-3 weeks.", "2026-07-19T21:23:00.000Z"),
    ]

    raw_path = write_raw(_thread(), messages, tmp_path)
    content = raw_path.read_text(encoding="utf-8")

    assert "How much slip are we talking?" in content
    assert "Legal thinks 2-3 weeks." in content
    assert "**Subject:** Original question" in content
    assert "<!-- appended" not in content


def test_write_raw_overwrites_rather_than_duplicating(tmp_path):
    """
    Regression test: raw.md must contain exactly the current full thread
    history on every write — a second call with an additional message must
    not leave the earlier messages duplicated in the file.
    """
    thread = _thread()
    first_pass = [
        _msg("a", "How much slip are we talking?", "2026-07-19T21:22:00.000Z"),
    ]
    write_raw(thread, first_pass, tmp_path)

    second_pass = first_pass + [
        _msg("b", "Reverse the decision!", "2026-07-24T23:32:00.000Z"),
    ]
    raw_path = write_raw(thread, second_pass, tmp_path)
    content = raw_path.read_text(encoding="utf-8")

    assert content.count("How much slip are we talking?") == 1
    assert content.count("Reverse the decision!") == 1
