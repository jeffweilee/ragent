"""UUIDv7 → 16 bytes → 26-char Crockford Base32 ID (spec §ID Generation Strategy)."""

import uuid_utils

# Crockford's Base32 alphabet: 0-9, A-Z minus I, L, O, U
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """Return a 26-character, sortable, Crockford-base32-encoded UUIDv7."""
    n = int.from_bytes(uuid_utils.uuid7().bytes, "big")
    chars: list[str] = []
    for _ in range(26):
        chars.append(_ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(chars))
