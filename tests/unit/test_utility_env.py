"""T-REDIS-AUTH.1 — parse_sentinel_hosts: host:port parsing pure function."""

from __future__ import annotations

import pytest

from ragent.utility.env import parse_sentinel_hosts


def test_single_host() -> None:
    assert parse_sentinel_hosts("s1:26379") == [("s1", 26379)]


def test_multiple_hosts() -> None:
    result = parse_sentinel_hosts("s1:26379,s2:26380")
    assert result == [("s1", 26379), ("s2", 26380)]


def test_whitespace_trimmed() -> None:
    result = parse_sentinel_hosts(" s1:26379 , s2:26380 ")
    assert result == [("s1", 26379), ("s2", 26380)]


def test_empty_entries_skipped() -> None:
    result = parse_sentinel_hosts("s1:26379,,s2:26380")
    assert result == [("s1", 26379), ("s2", 26380)]


def test_empty_string_returns_empty_list() -> None:
    assert parse_sentinel_hosts("") == []


def test_invalid_port_exits() -> None:
    with pytest.raises(SystemExit):
        parse_sentinel_hosts("s1:notaport")


def test_missing_port_exits() -> None:
    with pytest.raises(SystemExit):
        parse_sentinel_hosts("s1")
