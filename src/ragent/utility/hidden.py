"""Strip the `<hidden>…</hidden>` block from content surfaced to the client.

The upstream ChatAgent keeps conversation memory by `session` and persists
every message verbatim — including the `<hidden>` context/state preamble that
the v3 caller prepends to the user turn (`clients/adk_caller.py`). Because the
upstream replays persisted turns, that machine-supplied block can flow back out
through the v3 stream and the session history; both surfaces strip it here so it
never reaches the rendered conversation.

The matcher is intentionally lenient (whitespace / attribute variants, multi-line
bodies) to mirror the neutralization done when the block is *built*, and it
trims only the separator the preamble itself introduced — when no block is
present the text is returned unchanged so streaming deltas keep their own
leading/trailing whitespace.
"""

from __future__ import annotations

import re

# Match a whole `<hidden …>…</hidden …>` block plus any trailing whitespace
# (the `\n\n` separator the preamble adds before the user message).
_HIDDEN_BLOCK_RE = re.compile(
    r"<\s*hidden(?:\s+[^>]*)?>.*?<\s*/\s*hidden\s*>\s*",
    re.IGNORECASE | re.DOTALL,
)


def strip_hidden(text: str) -> str:
    return _HIDDEN_BLOCK_RE.sub("", text)
