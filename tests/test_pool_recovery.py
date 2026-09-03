"""Background recovery: what happens to a client that will not come back.

The case this guards is a restart that *succeeds* and changes nothing. The failure counter
only advances on an exception, so an account whose `init()` returns while the session stays
UNAUTHENTICATED was retried on the poll interval forever - measured in production at roughly
one pointless auth round-trip to Google per 67 seconds, indefinitely.
"""

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Any

import pytest

from app.services import pool as pool_module
from app.services.pool import (
    FUTILE_RESTART_BACKOFF_SECONDS,
    ClientBusyError,
    GeminiClientPool,
)


class FakeClient:
    def __init__(self, client_id: str, *, ready: bool = False):
        self.id = client_id
        self.ready = ready
        self.restarts = 0

    def running(self) -> bool:
        return True

    def is_healthy(self) -> bool:
        return self.ready

    def is_guest(self) -> bool:
        return not self.ready


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(pool_module.time, "monotonic", fake)
    return fake


class RecordingPool(GeminiClientPool):
    """A pool whose restart counts itself and does whatever the test says.

    Subclassing rather than replacing the bound method on the instance: an instance
    attribute would be called unbound, so its signature could never match the method
    it stands in for, and every type checker is right to say so.
    """

    restart_effect: Callable[[Any], None]

    async def _restart_client(self, client: Any) -> None:
        client.restarts += 1
        self.restart_effect(client)


def build_pool(clients, restart_effect):
    """A pool wired to `clients`, whose restarts do whatever `restart_effect` says."""
    pool = object.__new__(RecordingPool)
    pool.restart_effect = restart_effect
    pool._clients = list(clients)
    pool._id_map = {c.id: c for c in clients}
    pool._round_robin = deque(clients)
    pool._restart_locks = {c.id: asyncio.Lock() for c in clients}
    pool._retired = set()
    pool._restart_failures = {}
    pool._futile_restarts = {}
    pool._restart_not_before = {}
    pool._recovery_wanted = asyncio.Event()
    return pool


def test_a_restart_that_changes_nothing_backs_off_instead_of_spinning(clock):
    """Each futile round pushes the next attempt further out, up to the cap."""
    client = FakeClient("dead")
    pool = build_pool([client], lambda c: None)  # restart returns, client stays unusable

    for expected_delay in (*FUTILE_RESTART_BACKOFF_SECONDS, FUTILE_RESTART_BACKOFF_SECONDS[-1]):
        before = client.restarts
        asyncio.run(pool.recover_unavailable_clients())
        assert client.restarts == before + 1

        # Still inside the backoff window: the next pass must not touch it.
        clock.advance(expected_delay - 1)
        asyncio.run(pool.recover_unavailable_clients())
        assert client.restarts == before + 1, f"retried early, delay={expected_delay}"

        clock.advance(1)


def test_a_client_that_never_recovers_is_not_retired(clock):
    """Backoff, not retirement: `auto_refresh` can still rotate the cookies back to life."""
    client = FakeClient("dead")
    pool = build_pool([client], lambda c: None)

    for _ in range(10):
        asyncio.run(pool.recover_unavailable_clients())
        clock.advance(FUTILE_RESTART_BACKOFF_SECONDS[-1])

    assert pool.retired_clients == []
    assert client.restarts == 10


def test_recovery_clears_the_backoff_once_the_client_is_usable(clock):
    """A revived client starts from a clean slate, not mid-backoff."""
    client = FakeClient("flaky")
    pool = build_pool([client], lambda c: None)

    asyncio.run(pool.recover_unavailable_clients())
    asyncio.run(pool.recover_unavailable_clients())
    assert pool._futile_restarts["flaky"] >= 1

    client.ready = True
    asyncio.run(pool.recover_unavailable_clients())
    assert "flaky" not in pool._futile_restarts
    assert "flaky" not in pool._restart_not_before

    # Falls over again: it waits out the first tier, not the one it had climbed to.
    client.ready = False
    asyncio.run(pool.recover_unavailable_clients())
    assert pool._futile_restarts["flaky"] == 1


def test_a_restart_that_succeeds_is_reported_and_leaves_no_backoff(clock):
    client = FakeClient("recovering")

    def revive(c):
        c.ready = True

    pool = build_pool([client], revive)
    asyncio.run(pool.recover_unavailable_clients())

    assert client.ready
    assert pool._futile_restarts == {}
    assert pool._restart_not_before == {}


def test_a_raising_restart_still_counts_toward_retirement(clock):
    """Unchanged: a restart that actually fails is the case the counter was built for."""
    client = FakeClient("broken")

    def explode(_c):
        raise RuntimeError("auth refused")

    pool = build_pool([client], explode)
    limit = pool_module.g_config.gemini.restart_max_failures

    for _ in range(limit):
        asyncio.run(pool.recover_unavailable_clients())

    assert pool.retired_clients == ["broken"]
    # Retired clients are skipped entirely from here on.
    before = client.restarts
    asyncio.run(pool.recover_unavailable_clients())
    assert client.restarts == before


def test_a_busy_client_is_neither_counted_nor_deferred(clock):
    """Being mid-request is transient; it must not look like a failure or a futile round."""
    client = FakeClient("busy")

    def busy(_c):
        raise ClientBusyError("2 active requests")

    pool = build_pool([client], busy)
    asyncio.run(pool.recover_unavailable_clients())

    assert pool._restart_failures == {}
    assert pool._futile_restarts == {}
    assert pool._restart_not_before == {}
    assert pool.retired_clients == []


def test_a_healthy_client_is_left_alone(clock):
    client = FakeClient("fine", ready=True)
    pool = build_pool([client], lambda c: None)
    asyncio.run(pool.recover_unavailable_clients())
    assert client.restarts == 0


# --- acquire: how the pool reports being down ------------------------------------------
#
# A guest can never satisfy `_client_ready`, so the old second pass "falling back to a guest
# session" logged that line and then raised one line later. Callers now get told which way
# the pool is down, because expired cookies and a transient outage want opposite responses.


def _acquire(pool, **kwargs):
    return asyncio.run(pool.acquire(**kwargs))


def test_a_ready_client_is_returned():
    ready = FakeClient("ready", ready=True)
    pool = build_pool([FakeClient("guest"), ready], lambda c: None)
    assert _acquire(pool) is ready


def test_a_guest_is_never_handed_out():
    """Google gives a guest no history, no uploads and no model choice."""
    pool = build_pool([FakeClient("g1"), FakeClient("g2")], lambda c: None)
    for require_account in (False, True):
        with pytest.raises(RuntimeError):
            _acquire(pool, require_account=require_account)


@pytest.mark.parametrize("require_account", [False, True])
def test_all_cookies_expired_says_so_explicitly(require_account):
    pool = build_pool([FakeClient("g1"), FakeClient("g2")], lambda c: None)
    with pytest.raises(RuntimeError, match="unauthenticated") as caught:
        _acquire(pool, require_account=require_account)
    message = str(caught.value)
    assert "SECURE_1PSID" in message
    assert "guest" in message


def test_all_clients_retired_says_so_explicitly():
    clients = [FakeClient("a"), FakeClient("b")]
    pool = build_pool(clients, lambda c: None)
    pool._retired = {"a", "b"}
    with pytest.raises(RuntimeError, match="retired"):
        _acquire(pool)


def test_a_plain_outage_keeps_the_generic_message():
    """Not a credentials problem: something that a retry can still fix."""

    class Down(FakeClient):
        def is_guest(self):
            return False

    pool = build_pool([Down("down")], lambda c: None)
    with pytest.raises(RuntimeError, match="No Gemini clients are currently available"):
        _acquire(pool)


def test_acquire_by_id_still_checks_readiness():
    ready = FakeClient("ready", ready=True)
    pool = build_pool([ready, FakeClient("dead")], lambda c: None)
    assert asyncio.run(pool.acquire("ready")) is ready
    with pytest.raises(RuntimeError, match="not currently available"):
        asyncio.run(pool.acquire("dead"))
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(pool.acquire("nope"))


def test_round_robin_spreads_load_across_ready_clients():
    a, b = FakeClient("a", ready=True), FakeClient("b", ready=True)
    pool = build_pool([a, b], lambda c: None)
    picked = [_acquire(pool).id for _ in range(4)]
    assert picked.count("a") == 2
    assert picked.count("b") == 2
