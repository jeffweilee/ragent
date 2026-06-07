"""T-CAv3 — /chatagent/v3 smart-router (intent-based local LLM fast path or upstream proxy)."""

from __future__ import annotations

from dataclasses import dataclass, field

from ragent.utility.env import int_env, list_env, str_env

_DEFAULT_INTENT_PROMPT = (
    "Classify the user's latest message into exactly one intent label:\n"
    "  GREETING   — greetings, farewells, pleasantries, self-introductions\n"
    "  CHITCHAT   — casual conversation, emotional expression, small talk,\n"
    "               open-ended creative requests (poems, jokes, stories)\n"
    "               not tied to any document or knowledge base\n"
    "  QUESTION   — factual question requiring search or retrieval from\n"
    "               documents or a knowledge base\n"
    "  SUMMARY    — request to summarise document content\n"
    "  GENERATION — request to draft or write content grounded in documents;\n"
    "               pure creative writing with no document dependency → CHITCHAT\n"
    "Reply with only the label. No punctuation, no explanation."
)

_DEFAULT_SUFFICIENCY_PROMPT = (
    "You are a routing assistant. Given the conversation history and the\n"
    "user's latest message, decide whether the question can be fully answered\n"
    "using only the existing conversation history — without any new search,\n"
    "retrieval, or external tools.\n\n"
    "Reply YES only if the conversation history already contains sufficient\n"
    "information to answer the question accurately and completely.\n\n"
    "Reply NO if any of the following apply:\n"
    "- The user asks to search, find, look up, or retrieve information.\n"
    "- The user asks to repeat or extend a previous search\n"
    '  ("find more", "search again", "look further").\n'
    "- The answer requires information absent from the conversation history.\n"
    "- You are uncertain whether the history is sufficient.\n\n"
    "Reply with only YES or NO. No explanation."
)

_DEFAULT_FAST_PROMPT = (
    "You are a helpful, friendly assistant. Answer the user's message\n"
    "directly and naturally.\n\n"
    "When conversation history is provided, use it to maintain context\n"
    "and continuity across turns.\n\n"
    "If the question requires searching external documents or databases,\n"
    "honestly acknowledge that you need to look further — never fabricate\n"
    "information.\n\n"
    "Be concise. Respond in the same language as the user."
)

_DEFAULT_FAST_INTENTS: frozenset[str] = frozenset({"GREETING", "CHITCHAT"})
_DEFAULT_SESSION_HISTORY_LIMIT = 20


@dataclass
class _V3Config:
    fast_intents: frozenset[str] = field(default=_DEFAULT_FAST_INTENTS)
    session_history_limit: int = _DEFAULT_SESSION_HISTORY_LIMIT
    intent_prompt: str = _DEFAULT_INTENT_PROMPT
    sufficiency_prompt: str = _DEFAULT_SUFFICIENCY_PROMPT
    fast_prompt: str = _DEFAULT_FAST_PROMPT

    @classmethod
    def from_env(cls) -> _V3Config:
        parsed = list_env("CHATAGENT_V3_FAST_INTENTS")
        return cls(
            fast_intents=frozenset(parsed) if parsed else _DEFAULT_FAST_INTENTS,
            session_history_limit=int_env(
                "CHATAGENT_V3_SESSION_HISTORY_LIMIT", _DEFAULT_SESSION_HISTORY_LIMIT
            ),
            intent_prompt=str_env("CHATAGENT_V3_INTENT_PROMPT", _DEFAULT_INTENT_PROMPT),
            sufficiency_prompt=str_env(
                "CHATAGENT_V3_SUFFICIENCY_PROMPT", _DEFAULT_SUFFICIENCY_PROMPT
            ),
            fast_prompt=str_env("CHATAGENT_V3_FAST_PROMPT", _DEFAULT_FAST_PROMPT),
        )
