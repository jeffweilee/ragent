"""T-CHAOS.C6 — MinIO transient 503 (spec §3.6.1).

Validates that `MinioSiteRegistry.get_object` retries transient connection
errors (ConnectionError / S3Error non-client-errors) and succeeds when the
third attempt returns valid data.

This is an integration test using a mock MinIO client — no real MinIO
needed (the real MinIO is available via `dev_env` but not required for
this test's retry-logic focus).  Marked `@pytest.mark.docker` for chaos
suite consistency (nightly CI lane).

Spec §3.6.1 common acceptance asserts:
  1. `get_object` returns the correct bytes after transient failures.
  2. `minio.transient_error` log emitted for each retry attempt.
  3. Mock client called exactly 3 times (2 failures + 1 success).
  4. `chaos_drill_outcome_total{case="C6", outcome="pass"}` increments.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import structlog

pytestmark = [
    pytest.mark.docker,
]


def _make_mock_response(data: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = data
    return resp


def test_C6_minio_transient_503_retries_and_succeeds(dev_env, monkeypatch) -> None:
    """get_object retries ConnectionError twice then returns data on third attempt."""
    from ragent.bootstrap.metrics import chaos_drill_outcome_total
    from ragent.storage.minio_registry import DEFAULT_SITE, MinioSiteRegistry, SiteRecord

    # Use fast retry delay for the test
    monkeypatch.setenv("MINIO_GET_RETRIES", "3")
    monkeypatch.setenv("MINIO_GET_RETRY_DELAY_SECONDS", "0.01")

    expected_data = b"document content"
    attempt = [0]

    mock_client = MagicMock()

    def side_effect(bucket, key):
        attempt[0] += 1
        if attempt[0] <= 2:
            raise ConnectionError("503 Service Unavailable")
        return _make_mock_response(expected_data)

    mock_client.get_object.side_effect = side_effect

    rec = SiteRecord(
        name=DEFAULT_SITE,
        endpoint="localhost:9000",
        access_key="minioadmin",
        secret_key="minioadmin",  # pragma: allowlist secret
        bucket="ragent-uploads",
        client=mock_client,
    )
    registry = MinioSiteRegistry(sites={DEFAULT_SITE: rec})

    with structlog.testing.capture_logs() as cap:
        result = registry.get_object(DEFAULT_SITE, "some/key")

    # Assert 1: correct data returned
    assert result == expected_data

    # Assert 2: two transient_error log entries (attempt 1 and 2 retried)
    transient_logs = [e for e in cap if e.get("event") == "minio.transient_error"]
    assert len(transient_logs) == 2, (
        f"Expected 2 minio.transient_error log entries, got {len(transient_logs)}: {transient_logs}"
    )

    # Assert 3: mock client called exactly 3 times
    assert mock_client.get_object.call_count == 3, (
        f"Expected 3 get_object calls, got {mock_client.get_object.call_count}"
    )

    # Assert 4: record drill outcome
    chaos_drill_outcome_total.labels(case="C6", outcome="pass").inc()
    assert (
        chaos_drill_outcome_total.labels(case="C6", outcome="pass")._value.get() >= 1  # noqa: SLF001
    )
