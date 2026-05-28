"""Entry point: python -m twp_ai

Requires an LLMCaller to be configured. For the ragent-integrated deployment,
twp-ai is mounted directly into ragent's FastAPI app via create_router().

To run standalone, create a caller and pass it to create_app(), then start
uvicorn pointing at your own app factory.
"""

raise NotImplementedError(
    "twp-ai has no built-in standalone caller. "
    "Provide an LLMCaller implementation and call create_app(caller) "
    "from your own entry point, or mount create_router(caller) "
    "into an existing FastAPI app."
)
