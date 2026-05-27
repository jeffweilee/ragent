"""T-CH.D1–D6 — _requires_retrieve() and _detect_intent() unit tests.
T-CH2.R1–R3 — _INTENT_TEMPERATURE and _compute_skip_retrieve() unit tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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


# ---------------------------------------------------------------------------
# T-CH2.R1 — _INTENT_TEMPERATURE mapping
# ---------------------------------------------------------------------------


def test_intent_temperature_mapping():
    """_INTENT_TEMPERATURE maps all known intents; unknown defaults to _DEFAULT_TEMPERATURE."""
    from ragent.routers.chat import _INTENT_TEMPERATURE

    assert _INTENT_TEMPERATURE["GREETING"] == pytest.approx(0.8)
    assert _INTENT_TEMPERATURE["CHITCHAT"] == pytest.approx(0.8)
    assert _INTENT_TEMPERATURE["QUESTION"] == pytest.approx(0.2)
    assert _INTENT_TEMPERATURE["SUMMARY"] == pytest.approx(0.2)
    assert _INTENT_TEMPERATURE["GENERATION"] == pytest.approx(0.7)
    # unknown intent should not be present (fallback handled at call site)
    assert "UNKNOWN_XYZ" not in _INTENT_TEMPERATURE


# ---------------------------------------------------------------------------
# T-CH2.R2 / R3 — _compute_skip_retrieve: context_mode × intent
# ---------------------------------------------------------------------------


def test_caller_mode_always_skips_retrieval():
    """context_mode='caller' always returns skip=True regardless of intent."""
    from ragent.routers.chat import _compute_skip_retrieve

    for intent in ("GREETING", "CHITCHAT", "QUESTION", "SUMMARY", "GENERATION"):
        assert _compute_skip_retrieve("caller", intent) is True, (
            f"caller mode should skip retrieval for intent={intent}"
        )


def test_force_mode_always_runs_retrieval():
    """context_mode='force' always returns skip=False regardless of intent."""
    from ragent.routers.chat import _compute_skip_retrieve

    for intent in ("GREETING", "CHITCHAT", "QUESTION", "SUMMARY", "GENERATION"):
        assert _compute_skip_retrieve("force", intent) is False, (
            f"force mode should run retrieval for intent={intent}"
        )


def test_auto_mode_delegates_to_intent():
    """context_mode='auto' delegates to _INTENT_REQUIRES_RETRIEVE."""
    from ragent.routers.chat import _compute_skip_retrieve

    assert _compute_skip_retrieve("auto", "GREETING") is True
    assert _compute_skip_retrieve("auto", "CHITCHAT") is True
    assert _compute_skip_retrieve("auto", "QUESTION") is False
    assert _compute_skip_retrieve("auto", "SUMMARY") is False
    assert _compute_skip_retrieve("auto", "GENERATION") is False
