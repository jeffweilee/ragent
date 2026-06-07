"""T-CAv3.S1 — _V3Config dataclass reads env vars with correct defaults."""

from __future__ import annotations

from ragent.routers.chatagent_v3 import _V3Config


def test_default_fast_intents():
    cfg = _V3Config()
    assert cfg.fast_intents == frozenset({"GREETING", "CHITCHAT"})


def test_default_session_history_limit():
    cfg = _V3Config()
    assert cfg.session_history_limit == 20


def test_default_prompts_are_non_empty():
    cfg = _V3Config()
    assert cfg.intent_prompt
    assert cfg.sufficiency_prompt
    assert cfg.fast_prompt


def test_fast_intents_env_override(monkeypatch):
    monkeypatch.setenv("CHATAGENT_V3_FAST_INTENTS", "GREETING,CHITCHAT,QUESTION")
    cfg = _V3Config.from_env()
    assert cfg.fast_intents == frozenset({"GREETING", "CHITCHAT", "QUESTION"})


def test_session_history_limit_env_override(monkeypatch):
    monkeypatch.setenv("CHATAGENT_V3_SESSION_HISTORY_LIMIT", "5")
    cfg = _V3Config.from_env()
    assert cfg.session_history_limit == 5


def test_intent_prompt_env_override(monkeypatch):
    monkeypatch.setenv("CHATAGENT_V3_INTENT_PROMPT", "Custom intent prompt.")
    cfg = _V3Config.from_env()
    assert cfg.intent_prompt == "Custom intent prompt."


def test_sufficiency_prompt_env_override(monkeypatch):
    monkeypatch.setenv("CHATAGENT_V3_SUFFICIENCY_PROMPT", "Custom sufficiency prompt.")
    cfg = _V3Config.from_env()
    assert cfg.sufficiency_prompt == "Custom sufficiency prompt."


def test_fast_prompt_env_override(monkeypatch):
    monkeypatch.setenv("CHATAGENT_V3_FAST_PROMPT", "Custom fast prompt.")
    cfg = _V3Config.from_env()
    assert cfg.fast_prompt == "Custom fast prompt."


def test_fast_intents_strips_whitespace(monkeypatch):
    monkeypatch.setenv("CHATAGENT_V3_FAST_INTENTS", " GREETING , CHITCHAT ")
    cfg = _V3Config.from_env()
    assert cfg.fast_intents == frozenset({"GREETING", "CHITCHAT"})
