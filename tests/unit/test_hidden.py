"""strip_hidden — remove `<hidden>…</hidden>` blocks from surfaced content.

The upstream ChatAgent persists every message we send it (memory by session),
including the `<hidden>` context/state preamble prepended to the user turn. That
block must never reach the client when the message is surfaced back (v3 stream
or session history), so it is stripped on the way out.
"""

from ragent.utility.hidden import strip_hidden


def test_strips_prefix_block_and_separator() -> None:
    text = "<hidden>\n<context>[]</context>\n<state>{}</state>\n</hidden>\n\nWhat is X?"
    assert strip_hidden(text) == "What is X?"


def test_no_block_is_left_untouched_including_whitespace() -> None:
    # v3 streams deltas; trimming a delta with no block would corrupt content.
    assert strip_hidden("Hello ") == "Hello "
    assert strip_hidden(" world") == " world"
    assert strip_hidden("plain") == "plain"


def test_bare_block_becomes_empty() -> None:
    assert strip_hidden("<hidden>\n<state>{}</state>\n</hidden>") == ""


def test_whitespace_and_attribute_tag_variants_are_stripped() -> None:
    assert strip_hidden('<hidden attr="1">x</hidden >\n\ntail') == "tail"


def test_multiline_block_is_stripped() -> None:
    text = "<hidden>\nline 1\nline 2\n</hidden>\n\nanswer"
    assert strip_hidden(text) == "answer"


def test_multiple_blocks_all_stripped() -> None:
    assert strip_hidden("<hidden>a</hidden>mid<hidden>b</hidden>end") == "midend"
