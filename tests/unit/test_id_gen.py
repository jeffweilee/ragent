"""T0.3 — new_id() returns 26-char Crockford base32 UUIDv7; sortable across calls."""

import time

import pytest


def test_new_id_returns_26_chars() -> None:
    from ragent.utility.id_gen import new_id

    assert len(new_id()) == 26


def test_new_id_uses_crockford_alphabet() -> None:
    from ragent.utility.id_gen import new_id

    invalid = set("IiLlOoUu")  # Crockford excludes these
    for _ in range(20):
        assert not (set(new_id()) & invalid)


def test_new_id_sortable_across_calls() -> None:
    from ragent.utility.id_gen import new_id

    ids: list[str] = []
    for _ in range(50):
        ids.append(new_id())
        time.sleep(0)  # yield; ms-resolution still monotonic within same process
    assert ids == sorted(ids), "IDs must be lexicographically monotone"


def test_new_id_unique() -> None:
    from ragent.utility.id_gen import new_id

    sample = [new_id() for _ in range(200)]
    assert len(set(sample)) == 200


@pytest.mark.parametrize("_", range(5))
def test_new_id_length_always_26(_: int) -> None:
    from ragent.utility.id_gen import new_id

    assert len(new_id()) == 26
