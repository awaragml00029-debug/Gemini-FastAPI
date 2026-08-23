import asyncio

import pytest
from fastapi import Response

from app.server import health
from app.utils.config import GeminiConfig, GuestMode


class _Pool:
    def __init__(self, client_status: dict[str, bool], retired: list[str] | None = None):
        self._client_status = client_status
        self._retired = retired or []

    def status(self) -> dict[str, bool]:
        return self._client_status

    @property
    def retired_clients(self) -> list[str]:
        return sorted(self._retired)


class _Store:
    def stats(self) -> dict[str, int]:
        return {"entries": 1}


@pytest.mark.parametrize(
    ("guest_mode", "client_status", "expected_status", "expected_ok"),
    [
        (GuestMode.STRICT, {"healthy": True, "guest": False}, 503, False),
        (GuestMode.ADAPTIVE, {"healthy": True, "guest": False}, 200, True),
        (GuestMode.ADAPTIVE, {"guest-a": False, "guest-b": False}, 503, False),
        (GuestMode.PERMISSIVE, {"guest-a": False, "guest-b": False}, 200, True),
    ],
)
def test_guest_mode_controls_client_health_status(
    monkeypatch,
    guest_mode: GuestMode,
    client_status: dict[str, bool],
    expected_status: int,
    expected_ok: bool,
):
    monkeypatch.setattr(health, "GeminiClientPool", lambda: _Pool(client_status))
    monkeypatch.setattr(health, "LMDBConversationStore", _Store)
    monkeypatch.setattr(health.g_config.gemini, "guest_mode", guest_mode)
    response = Response()

    result = asyncio.run(health.health_check(response))

    assert response.status_code == expected_status
    assert result.ok is expected_ok


@pytest.mark.parametrize(
    ("client_status", "retired", "expected_status", "expected_ok", "expected_retired"),
    [
        # A retired client reads as false in `clients` like any other unhealthy one, so it is
        # listed separately: it is the only state that needs a human to fix the config and
        # restart the container. It must not by itself make the service look unhealthy.
        ({"a": True, "b": True, "c": False}, ["c"], 200, True, ["c"]),
        ({"a": True, "b": False}, [], 200, True, None),
        ({"a": False, "b": False}, ["a", "b"], 503, False, ["a", "b"]),
    ],
)
def test_retired_clients_are_reported_without_changing_ok(
    monkeypatch,
    client_status: dict[str, bool],
    retired: list[str],
    expected_status: int,
    expected_ok: bool,
    expected_retired: list[str] | None,
):
    monkeypatch.setattr(health, "GeminiClientPool", lambda: _Pool(client_status, retired))
    monkeypatch.setattr(health, "LMDBConversationStore", _Store)
    monkeypatch.setattr(health.g_config.gemini, "guest_mode", GuestMode.ADAPTIVE)
    response = Response()

    result = asyncio.run(health.health_check(response))

    assert response.status_code == expected_status
    assert result.ok is expected_ok
    assert (list(result.retired) if result.retired else None) == expected_retired


def test_guest_mode_defaults_to_adaptive():
    config = GeminiConfig(clients=[], auto_refresh=True, verbose=True)

    assert config.guest_mode == GuestMode.ADAPTIVE
