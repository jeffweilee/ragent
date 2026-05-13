"""Unit tests: chat router passes top_k and min_score to run_retrieval (B-Phase)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from ragent.routers.chat import create_chat_router


def _make_app():
    retrieval_pipeline = MagicMock()
    llm_client = MagicMock()
    llm_client.chat.return_value = {
        "content": "ok",
        "usage": {"promptTokens": 1, "completionTokens": 1, "totalTokens": 2},
    }
    app = FastAPI()
    app.include_router(create_chat_router(retrieval_pipeline=retrieval_pipeline, llm_client=llm_client))
    return app


def _capture_client(app, monkeypatch):
    calls: list = []

    def _run(*_a, **kw):
        calls.append(kw)
        return []

    monkeypatch.setattr("ragent.routers.chat.run_retrieval", _run)
    return TestClient(app), calls


def test_chat_passes_top_k_to_run_retrieval(monkeypatch):
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "top_k": 7})
    assert calls, "run_retrieval was not called"
    assert calls[0].get("top_k") == 7


def test_chat_passes_min_score_to_run_retrieval(monkeypatch):
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "min_score": 0.4})
    assert calls, "run_retrieval was not called"
    assert calls[0].get("min_score") == pytest.approx(0.4)


def test_chat_top_k_defaults_to_DEFAULT_TOP_K_when_omitted(monkeypatch):
    from ragent.pipelines.chat import DEFAULT_TOP_K
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}]})
    assert calls[0].get("top_k") == DEFAULT_TOP_K


def test_chat_min_score_defaults_to_DEFAULT_MIN_SCORE_when_omitted(monkeypatch):
    from ragent.pipelines.chat import DEFAULT_MIN_SCORE
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}]})
    assert calls[0].get("min_score") == DEFAULT_MIN_SCORE


def test_chat_top_k_validation_rejects_zero(monkeypatch):
    app = _make_app()
    client, _ = _capture_client(app, monkeypatch)
    resp = client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "top_k": 0})
    assert resp.status_code == 422


def test_chat_top_k_validation_rejects_over_200(monkeypatch):
    app = _make_app()
    client, _ = _capture_client(app, monkeypatch)
    resp = client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "top_k": 201})
    assert resp.status_code == 422


def test_chat_min_score_validation_rejects_negative(monkeypatch):
    app = _make_app()
    client, _ = _capture_client(app, monkeypatch)
    resp = client.post("/chat/v1", json={"messages": [{"role": "user", "content": "hi"}], "min_score": -0.5})
    assert resp.status_code == 422


def test_stream_passes_top_k_and_min_score_to_run_retrieval(monkeypatch):
    """Streaming endpoint shares _run_retrieval — same routing applies."""
    app = _make_app()
    client, calls = _capture_client(app, monkeypatch)
    client.post(
        "/chat/v1/stream",
        json={"messages": [{"role": "user", "content": "hi"}], "top_k": 3, "min_score": 0.2},
    )
    assert calls, "run_retrieval was not called for streaming endpoint"
    assert calls[0].get("top_k") == 3
    assert calls[0].get("min_score") == pytest.approx(0.2)
