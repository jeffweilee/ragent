"""T-CH.D1–D6 — _requires_retrieve() and _detect_intent() unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# T-CH.D1 — _requires_retrieve(): known intents
# ---------------------------------------------------------------------------


def test_requires_retrieve_known_intents():
    from ragent.routers.chat import _requires_retrieve

    assert _requires_retrieve("GREETING") is False
    assert _requires_retrieve("CHITCHAT") is False
    assert _requires_retrieve("QUESTION") is True
    assert _requires_retrieve("SUMMARY") is True
    assert _requires_retrieve("GENERATION") is True


# ---------------------------------------------------------------------------
# T-CH.D2 — _requires_retrieve(): unknown label → True (fail-safe)
# ---------------------------------------------------------------------------


def test_requires_retrieve_unknown_defaults_true():
    from ragent.routers.chat import _requires_retrieve

    assert _requires_retrieve("TRANSLATE") is True
    assert _requires_retrieve("") is True
    assert _requires_retrieve("FOOBAR") is True


# ---------------------------------------------------------------------------
# T-CH.D3 — _detect_intent(): exact known label
# ---------------------------------------------------------------------------


def test_detect_intent_known_label():
    from ragent.routers.chat import _detect_intent

    llm = MagicMock()
    llm.chat.return_value = {"content": "GREETING"}
    assert _detect_intent(llm, "你好", "gptoss-120b") == "GREETING"

    llm.chat.return_value = {"content": "CHITCHAT"}
    assert _detect_intent(llm, "haha", "gptoss-120b") == "CHITCHAT"

    llm.chat.return_value = {"content": "QUESTION"}
    assert _detect_intent(llm, "what is RAG?", "gptoss-120b") == "QUESTION"

    llm.chat.return_value = {"content": "SUMMARY"}
    assert _detect_intent(llm, "summarise this", "gptoss-120b") == "SUMMARY"

    llm.chat.return_value = {"content": "GENERATION"}
    assert _detect_intent(llm, "draft a report", "gptoss-120b") == "GENERATION"


# ---------------------------------------------------------------------------
# T-CH.D4 — _detect_intent(): unrecognised label → QUESTION (fallback)
# ---------------------------------------------------------------------------


def test_detect_intent_unknown_label_fallback():
    from ragent.routers.chat import _INTENT_DEFAULT, _detect_intent

    llm = MagicMock()
    llm.chat.return_value = {"content": "TRANSLATE"}
    assert _detect_intent(llm, "translate this", "gptoss-120b") == _INTENT_DEFAULT

    llm.chat.return_value = {"content": ""}
    assert _detect_intent(llm, "...", "gptoss-120b") == _INTENT_DEFAULT


# ---------------------------------------------------------------------------
# T-CH.D5 — _detect_intent(): LLM exception → QUESTION (fail-safe)
# ---------------------------------------------------------------------------


def test_detect_intent_exception_fallback():
    from ragent.routers.chat import _INTENT_DEFAULT, _detect_intent

    llm = MagicMock()
    llm.chat.side_effect = RuntimeError("upstream error")
    assert _detect_intent(llm, "question?", "gptoss-120b") == _INTENT_DEFAULT


# ---------------------------------------------------------------------------
# T-CH.D6 — _detect_intent(): multi-word LLM output → first word used
# ---------------------------------------------------------------------------


def test_detect_intent_multiword_uses_first_word():
    from ragent.routers.chat import _detect_intent

    llm = MagicMock()
    # LLM echoes explanation after the label
    llm.chat.return_value = {"content": "GREETING this is a greeting"}
    assert _detect_intent(llm, "hi", "gptoss-120b") == "GREETING"

    llm.chat.return_value = {"content": "QUESTION the user is asking something"}
    assert _detect_intent(llm, "what?", "gptoss-120b") == "QUESTION"
