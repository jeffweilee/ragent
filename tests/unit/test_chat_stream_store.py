"""T-CAv3R.1 — ChatStreamStore: Redis Stream buffer for resumable v3 runs."""

from __future__ import annotations

from unittest.mock import MagicMock

import fakeredis
import redis as redis_lib

from ragent.clients.chat_stream_store import ChatStreamStore


def _store(**kwargs) -> ChatStreamStore:
    return ChatStreamStore(fakeredis.FakeStrictRedis(decode_responses=True), **kwargs)


def test_key_is_owner_scoped() -> None:
    # The owner (user_id) is part of the key so another user cannot reconnect to
    # a run by guessing its run_id.
    key = ChatStreamStore.key("alice", "thread_1", "run_1")
    assert key == "chatstream:alice:thread_1:run_1"
    assert ChatStreamStore.key("bob", "thread_1", "run_1") != key


def test_append_then_read_after_from_start_returns_frames_in_order() -> None:
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    id1 = store.append(key, "data: a\n\n")
    id2 = store.append(key, "data: b\n\n")

    entries = store.read_after(key, "0")
    assert entries == [(id1, "data: a\n\n"), (id2, "data: b\n\n")]


def test_read_after_is_exclusive_of_last_id() -> None:
    # Resume semantics: a client that already saw `id1` must only get frames
    # strictly after it (Last-Event-ID is exclusive).
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    id1 = store.append(key, "data: a\n\n")
    id2 = store.append(key, "data: b\n\n")

    entries = store.read_after(key, id1)
    assert [eid for eid, _ in entries] == [id2]


def test_mark_done_appends_eos_sentinel_and_sets_ttl() -> None:
    store = _store(ttl_seconds=300)
    key = ChatStreamStore.key("alice", "t", "r")
    store.append(key, "data: a\n\n")
    store.mark_done(key)

    entries = store.read_after(key, "0")
    assert entries[-1][1] is None  # terminal sentinel surfaces as a None frame
    assert store._redis.ttl(key) > 0  # noqa: SLF001 — asserting the TTL was set


def test_is_done_false_while_running_true_after_mark_done() -> None:
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    assert store.is_done(key) is False  # no entries yet
    store.append(key, "data: a\n\n")
    assert store.is_done(key) is False  # still running (last entry is a frame)
    store.mark_done(key)
    assert store.is_done(key) is True  # eos is now the last entry


def test_is_resumable_false_before_anything_true_after_append() -> None:
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    assert store.is_resumable(key) is False
    store.append(key, "data: a\n\n")
    assert store.is_resumable(key) is True


def test_is_resumable_true_when_only_the_producer_lock_is_held() -> None:
    # Startup race: the POST took the lock but the producer has not written its
    # first frame yet — the run is still reconnectable.
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    store.try_start(key)
    assert store.is_resumable(key) is True


def test_try_start_is_idempotent_first_caller_wins() -> None:
    # Two POSTs with the same run_id must only spawn one producer.
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    assert store.try_start(key) is True
    assert store.try_start(key) is False


def test_try_start_returns_none_when_redis_unavailable() -> None:
    # Signals the router to take the legacy connection-bound fallback.
    redis = MagicMock()
    redis.set.side_effect = redis_lib.ConnectionError("down")
    store = ChatStreamStore(redis)
    assert store.try_start(ChatStreamStore.key("a", "t", "r")) is None


def test_read_after_fail_soft_returns_empty_on_redis_error() -> None:
    redis = MagicMock()
    redis.xrange.side_effect = redis_lib.ConnectionError("down")
    store = ChatStreamStore(redis)
    assert store.read_after(ChatStreamStore.key("a", "t", "r"), "0") == []


def test_is_valid_cursor_accepts_sentinels_and_entry_ids_rejects_garbage() -> None:
    assert ChatStreamStore.is_valid_cursor(None) is True
    assert ChatStreamStore.is_valid_cursor("0") is True
    assert ChatStreamStore.is_valid_cursor("1718000000000-3") is True
    assert ChatStreamStore.is_valid_cursor("invalid-id") is False
    assert ChatStreamStore.is_valid_cursor("1718000000000") is False
    assert ChatStreamStore.is_valid_cursor("'; FLUSHALL") is False


def test_is_from_start_treats_truthy_sentinels_as_from_start() -> None:
    # "0" and "-" are truthy strings — a plain falsiness check would miss them.
    assert ChatStreamStore.is_from_start(None) is True
    assert ChatStreamStore.is_from_start("") is True
    assert ChatStreamStore.is_from_start("0") is True
    assert ChatStreamStore.is_from_start("-") is True
    assert ChatStreamStore.is_from_start("1718000000000-3") is False


def test_current_pointer_round_trips_and_is_owner_scoped() -> None:
    store = _store()
    assert store.get_current("alice", "t") is None
    store.set_current("alice", "t", "run_1")
    store.set_current("alice", "t", "run_2")  # latest wins
    assert store.get_current("alice", "t") == "run_2"
    assert store.get_current("bob", "t") is None  # another user never sees it


def test_current_pointer_cannot_collide_with_a_run_id_named_current() -> None:
    # The pointer uses a distinct prefix, not a suffix on the buffer key.
    store = _store()
    store.set_current("alice", "t", "run_1")
    key = ChatStreamStore.key("alice", "t", "current")
    store.append(key, "data: x\n\n")  # a buffer whose run_id is literally "current"
    assert store.get_current("alice", "t") == "run_1"  # untouched


def test_user_input_stash_round_trips() -> None:
    store = _store()
    key = ChatStreamStore.key("alice", "t", "r")
    assert store.get_user_input(key) is None
    store.stash_user_input(key, "what are the features?")
    assert store.get_user_input(key) == "what are the features?"


def test_current_and_stash_fail_soft_on_redis_error() -> None:
    redis = MagicMock()
    redis.set.side_effect = redis_lib.ConnectionError("down")
    redis.get.side_effect = redis_lib.ConnectionError("down")
    store = ChatStreamStore(redis)
    store.set_current("a", "t", "r")  # does not raise
    store.stash_user_input(ChatStreamStore.key("a", "t", "r"), "x")  # does not raise
    assert store.get_current("a", "t") is None
    assert store.get_user_input(ChatStreamStore.key("a", "t", "r")) is None
