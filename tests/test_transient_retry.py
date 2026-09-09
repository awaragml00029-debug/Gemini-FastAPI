"""Google's transient stream errors must cost a resend, not the whole request.

gemini_webapi enumerates only 1013/1037/1050/1052/1060 in its `ErrorCode`; 1097 and 1155
fall through its catch-all and are raised with no retry, even though Google's own web UI
surfaces both as transient "Something went wrong" failures. Observed in production: 25
APIErrors, all of them 1097 or 1155.

Resending is safe at this layer specifically because `_send_and_await_first_chunk` pulls
the first chunk here -- nothing has been committed to the client yet.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from gemini_webapi.exceptions import APIError

from app.server import chat as chat_module
from app.server.chat import _TRANSIENT_RETRY_ATTEMPTS, _transient_api_error_code

STREAM_ERROR = "Failed to generate contents (stream). Unknown API error code: {}. This might be a temporary Google service issue."


class FakeClient:
    """Only request_scope() is reached on the paths under test."""

    def __init__(self) -> None:
        self.scopes = 0

    @asynccontextmanager
    async def request_scope(self):
        self.scopes += 1
        yield


def _run(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> tuple[Any, int, FakeClient]:
    """Drive one call, handing `outcomes` to successive send attempts."""
    attempts = 0

    async def fake_send(session, text, *, files, stream, temporary):
        nonlocal attempts
        attempts += 1
        item = outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(chat_module, "_send_and_await_first_chunk", fake_send)
    client = FakeClient()

    async def call():
        return await chat_module._send_with_internal_fallback(
            pool=cast(Any, None),
            db=cast(Any, None),
            resolved_model="gemini-flash",
            session=cast(Any, "session"),
            client=cast(Any, client),
            current_input="hi",
            files=[],
            full_prepared_messages=[],
            stored_conversation=None,
            tmp_dir=Path("/tmp"),
            stream=False,
            temporary=False,
        )

    return asyncio.run(call()), attempts, client


@pytest.mark.parametrize("code", [1097, 1155])
def test_transient_error_is_resent_and_succeeds(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    """One blip, then a clean answer: the caller never sees the error."""
    outcomes = [APIError(STREAM_ERROR.format(code)), "OK"]
    (output, _session, _client), attempts, client = _run(monkeypatch, outcomes)
    assert output == "OK"
    assert attempts == 2
    assert client.scopes == 2


@pytest.mark.parametrize("code", [1097, 1155])
def test_transient_error_stops_after_the_attempt_budget(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A backend that keeps failing must surface, not loop."""
    outcomes = [APIError(STREAM_ERROR.format(code)) for _ in range(_TRANSIENT_RETRY_ATTEMPTS + 2)]
    with pytest.raises(APIError):
        _run(monkeypatch, outcomes)
    assert len(outcomes) == 2  # exactly _TRANSIENT_RETRY_ATTEMPTS consumed


def test_unknown_code_is_not_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised code is a real failure; resending would just burn quota."""
    outcomes = [APIError(STREAM_ERROR.format(9999)), "OK"]
    with pytest.raises(APIError):
        _run(monkeypatch, outcomes)
    assert len(outcomes) == 1  # only one attempt consumed


def test_unrelated_api_error_is_not_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes = [APIError("Something else went wrong"), "OK"]
    with pytest.raises(APIError):
        _run(monkeypatch, outcomes)
    assert len(outcomes) == 1


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (STREAM_ERROR.format(1097), 1097),
        (STREAM_ERROR.format(1155), 1155),
        (STREAM_ERROR.format(9999), None),
        # 1013 is already enumerated by the library and phrased differently, so it does
        # not reach this predicate; documented here so the difference is deliberate.
        ("Gemini encountered a temporary error (1013). Retrying...", None),
        ("some unrelated failure", None),
    ],
)
def test_error_code_extraction(message: str, expected: int | None) -> None:
    assert _transient_api_error_code(APIError(message)) == expected
