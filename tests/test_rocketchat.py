"""Tests for Rocket.Chat source adapter."""

from types import SimpleNamespace

from src.sources.rocketchat import RocketChatSource


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, params))
        if path == "/api/v1/chat.getMessage":
            return _Response({"message": {
                "_id": "root", "msg": "Original question",
                "ts": {"$date": 1_720_000_000_000},
                "u": {"_id": "u1", "name": "Alice"},
            }})
        return _Response({"messages": [{
            "_id": "reply", "tmid": "root", "msg": "A reply",
            "ts": {"$date": 1_720_000_060_000},
            "u": {"_id": "u2", "name": "Bob"},
        }]})


class _Limiter:
    def acquire(self):
        pass


def test_fetch_thread_messages_includes_top_level_post():
    source = object.__new__(RocketChatSource)
    source._config = SimpleNamespace(page_size=100, url="https://chat.example.test")
    source._client = _Client()
    source._limiter = _Limiter()
    channel = SimpleNamespace(name="general")

    messages = source.fetch_thread_messages(channel, "root")

    assert [(message.message_id, message.thread_id, message.body) for message in messages] == [
        ("root", "root", "Original question"),
        ("reply", "root", "A reply"),
    ]
    assert source._client.calls[0][0] == "/api/v1/chat.getMessage"


def test_history_with_my_reply_is_discovered_as_a_tracked_thread():
    source = object.__new__(RocketChatSource)
    source._config = SimpleNamespace(page_size=100, url="https://chat.example.test")
    source._client = _Client()
    source._limiter = _Limiter()
    channel = SimpleNamespace(name="general")
    me = SimpleNamespace(
        rocketchat=SimpleNamespace(user_id="u2", username="bob")
    )

    history = source.fetch_thread_messages(channel, "root")
    threads = source.discover_eligible_threads(channel, history, me, set())

    assert len(threads) == 1
    assert threads[0].thread_id == "root"
    assert threads[0].reason == "replied"
    assert threads[0].thread_subject == "Original question"
