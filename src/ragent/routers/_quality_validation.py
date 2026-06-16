"""T-CVQ — Admin quality validation stream helper.

Intercepts the /admin-quality-validation slash command inside the
chatagent/v3 POST handler.  Runs the configured question suite against
the live /chatagent/v3 and /chatagent/v3/session endpoints (self-HTTP),
relays the upstream SSE as-is (minus lifecycle wrappers), and appends a
validation summary TEXT_MESSAGE at the end.

All I/O uses the shared httpx.Client from the composition root — no extra
HTTP client is needed.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Generator

import httpx
import structlog
from twp_ai.events import (
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    to_sse,
)
from twp_ai.schemas import Message

from ragent.errors.codes import HttpErrorCode
from ragent.utility.id_gen import new_id
from ragent.utility.quality_validation_checker import (
    check_keywords_any,
    check_no_keywords,
    check_protocol,
    check_session_messages,
    collect_text,
    parse_sse_line,
)

logger = structlog.get_logger(__name__)

ADMIN_COMMAND = "/admin-quality-validation"
_RELAY_SKIP = frozenset({"RUN_STARTED", "RUN_FINISHED", "RUN_ERROR"})


# ---------------------------------------------------------------------------
# Public helpers consumed by chatagent_v3.py
# ---------------------------------------------------------------------------


def is_admin_validation_command(messages: list[Message]) -> bool:
    """True when the last user message is the admin validation slash command."""
    last = next((m for m in reversed(messages) if m.role == "user"), None)
    return (
        last is not None and isinstance(last.content, str) and last.content.strip() == ADMIN_COMMAND
    )


def load_questions(fixture_path: str) -> list[dict]:
    """Load question suite from a YAML file.  Returns empty list on error."""
    import yaml  # lazy import — only used at startup

    try:
        with open(fixture_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("questions", [])
    except Exception as exc:
        logger.error("quality_validation.fixture_load_error", path=fixture_path, error=str(exc))
        return []


# ---------------------------------------------------------------------------
# JWT admin claim check (soft — no signature verification)
# ---------------------------------------------------------------------------


def _decode_jwt_claim(auth_header: str, claim: str) -> str | None:
    """Extract a claim from a JWT without verifying the signature."""
    try:
        token = auth_header.removeprefix("Bearer ").strip()
        _, payload_b64, _ = token.split(".", 2)
        padding = (4 - len(payload_b64) % 4) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        return payload.get(claim)
    except Exception:
        return None


def is_admin_user(auth_header: str, admin_user_ids: list[str], jwt_claim: str) -> bool:
    """True if the JWT claim user_id is in the configured admin list."""
    if not admin_user_ids or not auth_header:
        return False
    user_id = _decode_jwt_claim(auth_header, jwt_claim)
    return user_id in admin_user_ids


# ---------------------------------------------------------------------------
# Self-HTTP calls
# ---------------------------------------------------------------------------


def _build_auth_headers(user_id: str, auth_header: str, jwt_header: str) -> dict[str, str]:
    headers: dict[str, str] = {"X-User-Id": user_id}
    if auth_header:
        headers[jwt_header] = auth_header
    return headers


def _call_chatagent_v3(
    http_client: httpx.Client,
    base_url: str,
    question: str,
    user_id: str,
    auth_header: str,
    jwt_header: str,
) -> tuple[list[dict], str]:
    """POST to /chatagent/v3, collect all SSE events. Returns (events, thread_id)."""
    run_id = new_id()
    thread_id = new_id()
    req = http_client.build_request(
        "POST",
        f"{base_url}/chatagent/v3",
        json={
            "runId": run_id,
            "threadId": thread_id,
            "messages": [{"id": new_id(), "role": "user", "content": question}],
            "tools": [],
            "state": None,
            "context": [],
            "forwardedProps": None,
        },
        headers=_build_auth_headers(user_id, auth_header, jwt_header),
    )
    resp = http_client.send(req, stream=True)
    resp.raise_for_status()

    events: list[dict] = []
    minted_thread_id = thread_id
    try:
        for line in resp.iter_lines():
            event = parse_sse_line(line)
            if event is None:
                continue
            events.append(event)
            if event.get("type") == "RUN_STARTED":
                minted_thread_id = event.get("threadId") or thread_id
    finally:
        resp.close()

    return events, minted_thread_id


_SESSION_RETRY_DELAYS = (1.0, 2.0, 4.0)


def _call_session(
    http_client: httpx.Client,
    base_url: str,
    thread_id: str,
    user_id: str,
    auth_header: str,
    jwt_header: str,
) -> list[dict]:
    """Retries with backoff because the upstream may persist the session
    asynchronously after the SSE stream closes (especially for tool-call
    heavy questions).
    """
    url = f"{base_url}/chatagent/v3/session"
    headers = _build_auth_headers(user_id, auth_header, jwt_header)

    for attempt, delay in enumerate((*_SESSION_RETRY_DELAYS, None), start=1):
        resp = http_client.get(url, params={"session": thread_id}, headers=headers, timeout=15.0)
        if resp.status_code != 200:
            logger.warning(
                "quality_validation.session.non_200",
                thread_id=thread_id,
                status=resp.status_code,
                attempt=attempt,
            )
            if delay is not None:
                time.sleep(delay)
            continue

        data = resp.json()
        messages = data if isinstance(data, list) else (data.get("messages") or [])
        if messages:
            return messages

        logger.warning(
            "quality_validation.session.empty",
            thread_id=thread_id,
            attempt=attempt,
            data_keys=list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
        if delay is not None:
            time.sleep(delay)

    return []


# ---------------------------------------------------------------------------
# Text-block injection & event relay
# ---------------------------------------------------------------------------


def _yield_text(message_id: str, content: str) -> Generator[str, None, None]:
    yield to_sse(TextMessageStartEvent(message_id=message_id))
    yield to_sse(TextMessageContentEvent(message_id=message_id, delta=content))
    yield to_sse(TextMessageEndEvent(message_id=message_id))


def _yield_run_error(
    run_id: str, thread_id: str, message: str, code: str
) -> Generator[str, None, None]:
    yield to_sse(RunStartedEvent(run_id=run_id, thread_id=thread_id))
    yield to_sse(RunErrorEvent(run_id=run_id, thread_id=thread_id, message=message, code=code))


def _relay_events(events: list[dict]) -> Generator[str, None, None]:
    """Yield SSE strings for all non-lifecycle events (original JSON, not re-serialised)."""
    for event in events:
        if event.get("type") in _RELAY_SKIP:
            continue
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _check_row(lines: list[str], label: str, passed: bool, details: list[str]) -> None:
    lines.append(f"   {'✅' if passed else '❌'} {label}")
    for d in details:
        lines.append(f"      - {d}")


def _build_summary(
    questions: list[dict],
    stream_results: list[tuple[bool, list[str], list[str]]],
    session_results: list[tuple[bool, list[str], int]],
    elapsed_ms: int,
) -> str:
    total = len(questions)
    overall_passed = sum(
        1
        for (s_ok, _, _), (p_ok, _, _) in zip(stream_results, session_results, strict=False)
        if s_ok and p_ok
    )

    lines = [
        "\n━━━ 驗收摘要 ━━━",
        f"總題數：{total}　通過：{overall_passed}　失敗：{total - overall_passed}",
        f"耗時：{elapsed_ms} ms\n",
    ]

    for q, (s_ok, proto_violations, kw_violations), (p_ok, p_reasons, msg_count) in zip(
        questions, stream_results, session_results, strict=False
    ):
        icon = "✅" if (s_ok and p_ok) else "❌"
        lines.append(f"{icon} {q['id'].upper()} — {q['label']}")

        question_text = q.get("question", "")
        if len(question_text) > 80:
            question_text = question_text[:77] + "…"
        lines.append(f"   問：{question_text}")

        kw_any = q.get("expect_keywords_any", [])
        kw_no = q.get("expect_no_keywords", [])
        if kw_any:
            lines.append(f"   期望含有：{', '.join(kw_any)}")
        if kw_no:
            lines.append(f"   期望不含：{', '.join(kw_no)}")

        _check_row(lines, "Protocol", not proto_violations, proto_violations)

        if kw_any or kw_no:
            _check_row(lines, "Stream 關鍵字", not kw_violations, kw_violations)

        session_label = f"Session {msg_count} 則訊息" if msg_count else "Session 無訊息"
        _check_row(lines, session_label, p_ok, p_reasons)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------


def admin_quality_validation_stream(
    *,
    questions: list[dict],
    user_id: str,
    auth_header: str,
    http_client: httpx.Client,
    base_url: str,
    run_id: str,
    thread_id: str,
    admin_user_ids: list[str],
    jwt_claim: str,
    jwt_header: str,
    has_session_endpoint: bool,
) -> Generator[str, None, None]:
    """
    Main generator for the /admin-quality-validation slash command.

    Lifecycle:
      RUN_STARTED → [per-question: inject user msg, relay agent SSE] →
      [summary TEXT_MESSAGE] → RUN_FINISHED
    """
    if not is_admin_user(auth_header, admin_user_ids, jwt_claim):
        yield from _yield_run_error(
            run_id,
            thread_id,
            "Forbidden: caller is not a configured admin user",
            HttpErrorCode.QUALITY_VALIDATION_FORBIDDEN,
        )
        return

    if not questions:
        yield from _yield_run_error(
            run_id,
            thread_id,
            "No questions configured in quality_validation.yaml",
            HttpErrorCode.QUALITY_VALIDATION_NOT_CONFIGURED,
        )
        return

    base_url = base_url.rstrip("/")
    yield to_sse(RunStartedEvent(run_id=run_id, thread_id=thread_id))

    logger.info("quality_validation.run.started", user_id=user_id, total_questions=len(questions))
    start_time = time.monotonic()

    stream_results: list[tuple[bool, list[str], list[str]]] = []
    thread_ids: list[str] = []

    for q in questions:
        question_text = q["question"]

        yield from _yield_text(new_id(), f"User: {question_text}")

        try:
            events, q_thread_id = _call_chatagent_v3(
                http_client, base_url, question_text, user_id, auth_header, jwt_header
            )
        except Exception as exc:
            logger.error("quality_validation.chatagent_error", question_id=q["id"], error=str(exc))
            stream_results.append((False, [f"HTTP error calling /chatagent/v3: {exc}"], []))
            thread_ids.append("")
            continue

        thread_ids.append(q_thread_id)
        yield from _relay_events(events)

        proto_violations = check_protocol(
            events, expect_no_tool_calls=q.get("expect_no_tool_calls", False)
        )
        text = collect_text(events)
        kw_violations = check_keywords_any(text, q.get("expect_keywords_any", []))
        kw_violations += check_no_keywords(text, q.get("expect_no_keywords", []))
        passed = not proto_violations and not kw_violations
        stream_results.append((passed, proto_violations, kw_violations))

        logger.info(
            "quality_validation.run.question_result",
            question_id=q["id"],
            stream_passed=passed,
        )

    session_results: list[tuple[bool, list[str], int]] = []

    for q, q_thread_id in zip(questions, thread_ids, strict=False):
        if not has_session_endpoint or not q_thread_id:
            reason = (
                "session endpoint not configured" if not has_session_endpoint else "no thread_id"
            )
            session_results.append((False, [reason], 0))
            continue

        try:
            messages = _call_session(
                http_client, base_url, q_thread_id, user_id, auth_header, jwt_header
            )
        except Exception as exc:
            session_results.append((False, [f"HTTP error calling /chatagent/v3/session: {exc}"], 0))
            continue

        if not messages:
            session_results.append(
                (False, ["session returned no messages (HTTP error or empty)"], 0)
            )
            continue

        violations = check_session_messages(messages, keywords_any=q.get("expect_keywords_any", []))
        session_results.append((not violations, violations, len(messages)))

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    summary = _build_summary(questions, stream_results, session_results, elapsed_ms)
    yield from _yield_text(new_id(), summary)

    logger.info(
        "quality_validation.run.finished",
        user_id=user_id,
        elapsed_ms=elapsed_ms,
    )
    yield to_sse(RunFinishedEvent(run_id=run_id, thread_id=thread_id))
