"""Our close() override must keep pace with the library's cookie-cache contract.

gemini_webapi (luuquangvu/Gemini-API 42f89a7) inserted `base_psid` as the second
parameter of `save_cookies` and pins the cache file to the PSID the client started with,
so a rotated PSID cannot fork the entry. Our close() replaces the library's wholesale, so
it inherits neither change on its own: passing `verbose` positionally, as it used to, hands
a bool to the cache-path hasher and close() raises on every authenticated shutdown.
"""

import asyncio
from typing import Any

import pytest
from gemini_webapi.constants import AccountStatus

from app.services import client as client_module
from app.services.client import GeminiClientWrapper


@pytest.fixture
def saved(monkeypatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        client_module, "save_cookies", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def _running_client(**kwargs: Any) -> GeminiClientWrapper:
    client = GeminiClientWrapper("acc-1", **kwargs)
    client._running = True
    client.account_status = AccountStatus.AVAILABLE
    return client


def test_close_pins_the_cache_to_the_base_psid_by_keyword(saved):
    """The base PSID is passed by name, and verbose never lands in its slot."""
    client = _running_client(secure_1psid="base-psid", secure_1psidts="ts")
    client.verbose = True

    asyncio.run(client.close())

    assert len(saved) == 1
    args, kwargs = saved[0]
    assert args == (client._cookies,)
    assert kwargs == {"base_psid": "base-psid", "verbose": True}


def test_close_skips_the_cache_write_without_any_psid(saved):
    """Nothing identifies the cache entry, so there is nothing safe to write."""
    client = _running_client()

    asyncio.run(client.close())

    assert saved == []


def test_close_still_skips_the_cache_write_when_the_client_never_ran(saved):
    """The existing guard survives the signature change."""
    client = _running_client(secure_1psid="base-psid")
    client._running = False

    asyncio.run(client.close())

    assert saved == []
