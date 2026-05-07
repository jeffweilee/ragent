"""Shared E2E helpers: process spawn, API readiness, common URL."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator

import httpx
import pytest

API_URL = "http://127.0.0.1:8000"


@pytest.fixture
def e2e_env(dev_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """E2E env layered on the integration `dev_env` fixture."""
    monkeypatch.setenv("RAGENT_PORT", "8000")


@pytest.fixture
def spawn_module() -> Iterator[callable]:
    """Spawn `python -m <module>` subprocesses; auto-terminate on test exit."""
    procs: list[subprocess.Popen] = []

    def _spawn(module: str) -> subprocess.Popen:
        log_path = f"/tmp/e2e_{module.replace('.', '_')}.log"
        out = open(log_path, "w")  # noqa: SIM115 — fd lifetime tied to procs list
        proc = subprocess.Popen(
            [sys.executable, "-m", module],
            env={**os.environ},
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        procs.append(proc)
        return proc

    yield _spawn

    for p in procs:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def wait_api_ready(timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{API_URL}/livez", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError("API never reached /livez=200")
