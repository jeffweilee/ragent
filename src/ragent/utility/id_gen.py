"""UUIDv7 → 16 bytes → 26-char Crockford Base32 ID (spec §ID Generation Strategy)."""

import uuid_utils

# Crockford's Base32 alphabet: 0-9, A-Z minus I, L, O, U
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    n = int.from_bytes(uuid_utils.uuid7().bytes, "big")
    return "".join(_ALPHABET[(n >> (i * 5)) & 0x1F] for i in range(25, -1, -1))
